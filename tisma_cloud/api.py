"""The master server: the API of the users (Tisma, the app), of the computing nodes and of the administrator."""

import asyncio
import json
import logging
import hmac
import ipaddress
from contextlib import asynccontextmanager
from importlib import resources
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from . import __version__, scheduler
from .db import Database
from .jobs import JOB_CLASS_DESCRIPTIONS, JOB_CLASSES, JobError, count_triangles, plan_job
from .models import ApiToken, Job, Node, Plan, User, utcnow
from .ratelimit import RateLimiter
from .security import DUMMY_HASH, hash_password, hash_token, new_token, verify_password
from .settings import Settings
from .storage import ALLOWED_RESULTS, Storage, TooLarge, safe_filename

log = logging.getLogger("tisma_cloud")


class Services:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(settings.database_url)
        self.db.create_all()
        self.storage = Storage(settings.storage_dir)
        self.limiter = RateLimiter()

    def sweep(self) -> None:
        """Requeues the jobs of lost nodes and removes the files of old jobs."""
        with self.db.session() as s:
            scheduler.requeue_stale(s, self.settings.node_timeout_s, self.settings.max_attempts)
        limit = utcnow() - timedelta(hours=self.settings.retention_h)
        with self.db.session() as s:
            old = s.execute(
                select(Job).where(Job.status.in_(scheduler.FINAL), Job.finished_at < limit, Job.files_removed.is_(False))
            ).scalars().all()
            for job in old:
                self.storage.remove_job(job.id)
                job.files_removed = True


def job_view(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "kind": job.kind,
        "job_class": job.job_class,
        "params": job.params_dict,
        "status": job.status,
        "progress": job.progress,
        "seconds": round(job.seconds, 1),
        "error": job.error,
        "summary": job.summary_dict,
        "result_filename": job.result_filename if job.status == "done" and not job.files_removed else "",
        "created_at": job.created_at.isoformat() + "Z",
        "started_at": job.started_at.isoformat() + "Z" if job.started_at else None,
        "finished_at": job.finished_at.isoformat() + "Z" if job.finished_at else None,
    }


def node_view(node: Node, running: int = 0, timeout_s: int = 90) -> dict[str, Any]:
    return {
        "name": node.name,
        "priorities": node.priority_list,
        "enabled": node.enabled,
        "info": node.info_dict,
        "last_seen": node.last_seen.isoformat() + "Z" if node.last_seen else None,
        # Seen recently: the node is waiting for jobs or running them (it claims or beats every few seconds).
        "online": node.last_seen is not None and utcnow() - node.last_seen < timedelta(seconds=timeout_s),
        "running": running,
    }


def user_view(user: User) -> dict[str, Any]:
    return {"email": user.email, "external_id": user.external_id, "plan": user.plan_name,
            "paid_until": user.paid_until.isoformat() + "Z" if user.paid_until else None, "enabled": user.enabled,
            "is_admin": user.is_admin}


def plan_view(plan: Plan) -> dict[str, Any]:
    return {"name": plan.name, "monthly_hours": plan.monthly_hours, "max_concurrent": plan.max_concurrent,
            "requests_per_minute": plan.requests_per_minute, "priority": plan.priority}


def parse_networks(text: str) -> list:
    return [ipaddress.ip_network(n.strip(), strict=False) for n in text.split(",") if n.strip()]


def address_in(address: str, networks: list) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in n for n in networks)


def bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing access token", headers={"WWW-Authenticate": "Bearer"})
    return authorization[len("Bearer "):].strip()


def check_priorities(priorities: list[str]) -> str:
    unknown = [p for p in priorities if p not in JOB_CLASSES]
    if unknown:
        raise HTTPException(422, f"Unknown job classes: {', '.join(unknown)} (known: {', '.join(JOB_CLASSES)})")
    if len(set(priorities)) != len(priorities):
        raise HTTPException(422, "A job class is repeated")
    return ",".join(priorities)


class ClaimBody(BaseModel):
    info: dict[str, Any] = Field(default_factory=dict)


class HeartbeatBody(BaseModel):
    running: list[str] = Field(default_factory=list)
    info: dict[str, Any] = Field(default_factory=dict)


class ProgressBody(BaseModel):
    progress: int = Field(ge=0, le=100)


class FailBody(BaseModel):
    error: str = Field(max_length=4000)
    cancelled: bool = False


class NodeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    priorities: list[str]


class NodeUpdate(BaseModel):
    priorities: list[str] | None = None
    enabled: bool | None = None


class LoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)
    # Name of the device, to tell the sessions apart ("Tisma on the office PC").
    device: str = Field(default="", max_length=100)


class PlanBody(BaseModel):
    monthly_hours: float = Field(ge=0)
    max_concurrent: int = Field(ge=1, le=100)
    requests_per_minute: int = Field(default=60, ge=1, le=10000)
    priority: int = 0


class SiteUser(BaseModel):
    """A user as the web site sends it (create or update; omitted fields keep their value)."""

    email: str | None = Field(default=None, min_length=3, max_length=200)
    password: str | None = Field(default=None, min_length=8, max_length=200)
    plan: str | None = None
    paid_until: datetime | None = None
    enabled: bool | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    services = Services(settings or Settings())

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async def sweeper() -> None:
            while True:
                try:
                    await run_in_threadpool(services.sweep)
                except Exception:  # noqa: BLE001 (keep sweeping)
                    log.exception("Sweep failed")
                await asyncio.sleep(30)

        task = asyncio.create_task(sweeper())
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(title="Tisma Cloud", version=__version__, lifespan=lifespan)
    app.state.services = services
    db = services.db
    cfg = services.settings

    limiter = services.limiter

    admin_networks = parse_networks(cfg.admin_networks)
    site_networks = parse_networks(cfg.site_networks)

    def client_key(request: Request) -> str:
        # Behind Caddy the address of the user is in X-Forwarded-For (Caddy replaces what the user sends). Without it, the
        # address of the connection (the local network reaches port 8000 directly).
        forwarded = request.headers.get("x-forwarded-for", "")
        return forwarded.split(",")[0].strip() or (request.client.host if request.client else "?")

    def local_only(request: Request) -> None:
        """The administration exists only for the local network: from internet it answers as an unknown page."""
        if not address_in(client_key(request), admin_networks):
            raise HTTPException(404, "Not Found")

    # ---- authentication
    def current_user(request: Request, authorization: Annotated[str | None, Header()] = None) -> User:
        token = bearer(authorization)
        with db.session() as s:
            row = s.execute(
                select(ApiToken, User).join(User, User.id == ApiToken.user_id).where(ApiToken.token_hash == hash_token(token))
            ).one_or_none()
            if row is None or not row[1].enabled:
                raise HTTPException(401, "Invalid access token", headers={"WWW-Authenticate": "Bearer"})
            api_token, user = row
            now = utcnow()
            if api_token.last_used is None or now - api_token.last_used > timedelta(minutes=5):
                api_token.last_used = now
            limit = 10**6 if user.is_admin else user.plan.requests_per_minute
        if not limiter.allow(f"user:{user.id}", limit):
            raise HTTPException(429, "Too many requests: wait a minute", headers={"Retry-After": "60"})
        request.state.token_hash = hash_token(token)
        return user

    def current_admin(_local: Annotated[None, Depends(local_only)], user: Annotated[User, Depends(current_user)]) -> User:
        if not user.is_admin:
            raise HTTPException(403, "Only for administrators")
        return user

    def current_node(authorization: Annotated[str | None, Header()] = None) -> Node:
        token = bearer(authorization)
        with db.session() as s:
            node = s.execute(select(Node).where(Node.token_hash == hash_token(token))).scalar_one_or_none()
            if node is None or not node.enabled:
                raise HTTPException(401, "Invalid node token", headers={"WWW-Authenticate": "Bearer"})
            node.last_seen = utcnow()
        if not limiter.allow(f"node:{node.id}", cfg.node_requests_per_minute):
            raise HTTPException(429, "Too many requests", headers={"Retry-After": "10"})
        return node

    def current_site(request: Request, authorization: Annotated[str | None, Header()] = None) -> None:
        if site_networks and not address_in(client_key(request), site_networks):
            raise HTTPException(404, "Not Found")
        token = bearer(authorization)
        if not cfg.site_key or not hmac.compare_digest(token.encode(), cfg.site_key.encode()):
            raise HTTPException(401, "Invalid site key", headers={"WWW-Authenticate": "Bearer"})

    UserDep = Annotated[User, Depends(current_user)]
    AdminDep = Annotated[User, Depends(current_admin)]
    NodeDep = Annotated[Node, Depends(current_node)]
    SiteDep = Annotated[None, Depends(current_site)]

    def own_job(s, user: User, job_id: str) -> Job:  # noqa: ANN001
        job = s.get(Job, job_id)
        if job is None or job.user_id != user.id:
            raise HTTPException(404, "No such job")
        return job

    def node_job(s, node: Node, job_id: str) -> Job:  # noqa: ANN001
        job = s.get(Job, job_id)
        if job is None or job.node_id != node.id or job.status != "running":
            raise HTTPException(404, "No such running job on this node")
        return job

    # ---- users
    @app.get("/v1/health")
    def health() -> dict:
        return {"ok": True, "service": "tisma-cloud", "version": __version__, "billing_url": cfg.billing_url}

    @app.post("/v1/auth/login")
    def login(request: Request, body: LoginBody) -> dict:
        email = body.email.strip().lower()
        if not (limiter.allow(f"login-ip:{client_key(request)}", cfg.login_per_minute)
                and limiter.allow(f"login-email:{email}", cfg.login_per_minute)):
            raise HTTPException(429, "Too many login attempts: wait a minute", headers={"Retry-After": "60"})
        with db.session() as s:
            user = s.execute(select(User).where(User.email == email)).scalar_one_or_none()
            ok = verify_password(body.password, user.password_hash if user and user.password_hash else DUMMY_HASH)
            if user is None or not ok or not user.enabled:
                raise HTTPException(401, "Wrong e-mail or password")
            token = new_token("tu")
            s.add(ApiToken(user_id=user.id, token_hash=hash_token(token), label=body.device))
            return {"token": token, "email": user.email}

    @app.post("/v1/auth/logout")
    def logout(user: UserDep, request: Request) -> dict:
        with db.session() as s:
            s.execute(ApiToken.__table__.delete().where(ApiToken.token_hash == request.state.token_hash))
        return {"ok": True}

    @app.get("/v1/me")
    def me(user: UserDep) -> dict:
        with db.session() as s:
            u = s.get(User, user.id)
            return {"email": u.email, **scheduler.account(s, u, cfg.grace_days).as_dict()}

    @app.get("/v1/plans")
    def plans() -> dict:
        with db.session() as s:
            rows = s.execute(select(Plan).order_by(Plan.monthly_hours)).scalars().all()
            return {"plans": [plan_view(p) for p in rows], "classes": JOB_CLASS_DESCRIPTIONS}

    @app.post("/v1/jobs", status_code=201)
    def submit(
        user: UserDep,
        request: Request,
        kind: Annotated[str, Form()],
        file: Annotated[UploadFile, File()],
        params: Annotated[str, Form()] = "{}",
    ) -> dict:
        max_bytes = cfg.max_upload_mb << 20
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > max_bytes + (1 << 20):
            raise HTTPException(413, f"The project is larger than {cfg.max_upload_mb} MB")
        try:
            parsed = json.loads(params)
        except ValueError:
            raise HTTPException(422, "'params' is not JSON") from None
        try:
            file.file.seek(0)
            # The triangles choose the queue of slicing; for an analysis it only checks that it is a 3MF project.
            triangles = count_triangles(file.file, cfg.large_slice_triangles if kind == "slice" else 0)
            plan = plan_job(kind, parsed, triangles, cfg.large_slice_triangles)
        except JobError as ex:
            raise HTTPException(422, str(ex)) from None
        with db.session() as s:
            u = s.get(User, user.id)
            try:
                scheduler.admit(s, u, cfg.grace_days)
            except scheduler.Refused as ex:
                raise HTTPException(ex.status, str(ex)) from None
            job = Job(user_id=u.id, kind=plan.kind, job_class=plan.job_class, params=json.dumps(plan.params),
                      priority=u.plan.priority, input_name=safe_filename(file.filename or "", "project.3mf"))
            s.add(job)
            s.flush()
            try:
                file.file.seek(0)
                services.storage.save_stream(file.file, services.storage.input_path(job.id), max_bytes)
            except TooLarge:
                services.storage.remove_job(job.id)
                raise HTTPException(413, f"The project is larger than {cfg.max_upload_mb} MB") from None
            return job_view(job)

    @app.get("/v1/jobs")
    def list_jobs(user: UserDep, limit: int = 50) -> dict:
        limit = max(1, min(limit, 200))
        with db.session() as s:
            jobs = s.execute(
                select(Job).where(Job.user_id == user.id).order_by(Job.created_at.desc()).limit(limit)
            ).scalars().all()
            return {"jobs": [job_view(j) for j in jobs]}

    @app.get("/v1/jobs/{job_id}")
    def get_job(user: UserDep, job_id: str) -> dict:
        with db.session() as s:
            return job_view(own_job(s, user, job_id))

    @app.get("/v1/jobs/{job_id}/result")
    def get_result(user: UserDep, job_id: str) -> FileResponse:
        with db.session() as s:
            job = own_job(s, user, job_id)
            if job.status != "done" or job.files_removed:
                raise HTTPException(409, "The job has no result")
            path = services.storage.find_result(job.id)
            if path is None:
                raise HTTPException(410, "The result was removed")
            return FileResponse(path, filename=job.result_filename or path.name, media_type="application/octet-stream")

    @app.post("/v1/jobs/{job_id}/cancel")
    def cancel_job(user: UserDep, job_id: str) -> dict:
        with db.session() as s:
            job = own_job(s, user, job_id)
            scheduler.cancel(s, job)
            return job_view(job)

    # ---- computing nodes
    def try_claim(node_id: int) -> dict | None:
        with db.session() as s:
            node = s.get(Node, node_id)
            if node is None or not node.enabled:
                return None
            job = scheduler.claim(s, node, db.is_sqlite)
            if job is None:
                return None
            return {"id": job.id, "kind": job.kind, "job_class": job.job_class, "params": job.params_dict,
                    "input_name": job.input_name}

    @app.post("/v1/node/claim")
    async def node_claim(node: NodeDep, body: ClaimBody, wait: int | None = None) -> Any:
        if body.info:
            def save_info() -> None:
                with db.session() as s:
                    n = s.get(Node, node.id)
                    if n is not None:
                        n.info = json.dumps(body.info)[:4000]
            await run_in_threadpool(save_info)
        wait_s = cfg.claim_wait_s if wait is None else max(0, min(wait, cfg.claim_wait_s))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        while True:
            job = await run_in_threadpool(try_claim, node.id)
            if job is not None:
                return {"job": job, "priorities": node.priority_list}
            if loop.time() >= deadline:
                return Response(status_code=204)
            await asyncio.sleep(1.)

    @app.post("/v1/node/heartbeat")
    def node_heartbeat(node: NodeDep, body: HeartbeatBody) -> dict:
        with db.session() as s:
            if body.info:
                n = s.get(Node, node.id)
                n.info = json.dumps(body.info)[:4000]
            cancel = []
            if body.running:
                jobs = s.execute(select(Job).where(Job.id.in_(body.running[:64]), Job.node_id == node.id)).scalars().all()
                # Jobs to stop: cancelled by the user, or no longer this node's (requeued after a timeout).
                known = {j.id for j in jobs if j.status == "running"}
                cancel = [j.id for j in jobs if j.cancel_requested] + [i for i in body.running if i not in known]
            return {"cancel": sorted(set(cancel)), "priorities": node.priority_list}

    @app.get("/v1/node/jobs/{job_id}/input")
    def node_input(node: NodeDep, job_id: str) -> FileResponse:
        with db.session() as s:
            job = node_job(s, node, job_id)
            path = services.storage.input_path(job.id)
        if not path.exists():
            raise HTTPException(410, "The project was removed")
        return FileResponse(path, media_type="application/octet-stream", filename="input.3mf")

    @app.post("/v1/node/jobs/{job_id}/progress")
    def node_progress(node: NodeDep, job_id: str, body: ProgressBody) -> dict:
        with db.session() as s:
            job = node_job(s, node, job_id)
            scheduler.set_progress(s, job, body.progress)
            return {"cancel": job.cancel_requested}

    @app.post("/v1/node/jobs/{job_id}/result")
    def node_result(
        node: NodeDep,
        job_id: str,
        file: Annotated[UploadFile, File()],
        summary: Annotated[str, Form()] = "{}",
    ) -> dict:
        try:
            summary_dict = json.loads(summary)
            if not isinstance(summary_dict, dict):
                raise ValueError
        except ValueError:
            raise HTTPException(422, "'summary' must be a JSON object") from None
        name = safe_filename(file.filename or "", "result")
        ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in ALLOWED_RESULTS:
            raise HTTPException(422, f"Results are {', '.join(ALLOWED_RESULTS)} files")
        with db.session() as s:
            job = node_job(s, node, job_id)
            try:
                services.storage.save_stream(file.file, services.storage.result_path(job.id, ext), cfg.max_result_mb << 20)
            except TooLarge:
                scheduler.fail(s, job, f"The result is larger than {cfg.max_result_mb} MB")
                raise HTTPException(413, "The result is too large") from None
            stem = job.input_name.rsplit(".", 1)[0] if job.input_name else "result"
            scheduler.finish(s, job, safe_filename(stem + ext, "result" + ext), summary_dict)
            return {"ok": True}

    @app.post("/v1/node/jobs/{job_id}/fail")
    def node_fail(node: NodeDep, job_id: str, body: FailBody) -> dict:
        with db.session() as s:
            job = node_job(s, node, job_id)
            if body.cancelled and job.cancel_requested:
                scheduler.stopped_by_node(s, job)
            else:
                scheduler.fail(s, job, body.error)
            return {"ok": True}

    # ---- administrator
    @app.get("/v1/admin/nodes")
    def admin_nodes(_admin: AdminDep) -> dict:
        with db.session() as s:
            running = dict(s.execute(
                select(Job.node_id, func.count()).where(Job.status == "running").group_by(Job.node_id)
            ).all())
            nodes = s.execute(select(Node).order_by(Node.name)).scalars().all()
            return {"nodes": [node_view(n, running.get(n.id, 0), cfg.node_timeout_s) for n in nodes],
                    "classes": list(JOB_CLASSES), "descriptions": JOB_CLASS_DESCRIPTIONS}

    @app.post("/v1/admin/nodes", status_code=201)
    def admin_add_node(_admin: AdminDep, body: NodeCreate) -> dict:
        priorities = check_priorities(body.priorities)
        token = new_token("tn")
        with db.session() as s:
            if s.execute(select(Node).where(Node.name == body.name)).scalar_one_or_none():
                raise HTTPException(409, "A node with that name exists")
            node = Node(name=body.name, token_hash=hash_token(token), priorities=priorities)
            s.add(node)
            s.flush()
            return {"node": node_view(node, 0, cfg.node_timeout_s), "token": token}

    @app.patch("/v1/admin/nodes/{name}")
    def admin_update_node(_admin: AdminDep, name: str, body: NodeUpdate) -> dict:
        with db.session() as s:
            node = s.execute(select(Node).where(Node.name == name)).scalar_one_or_none()
            if node is None:
                raise HTTPException(404, "No such node")
            if body.priorities is not None:
                node.priorities = check_priorities(body.priorities)
            if body.enabled is not None:
                node.enabled = body.enabled
            return {"node": node_view(node, 0, cfg.node_timeout_s)}

    @app.get("/v1/admin/users")
    def admin_users(_admin: AdminDep) -> dict:
        with db.session() as s:
            return {"users": [user_view(u) for u in s.execute(select(User).order_by(User.email)).scalars()]}

    # ---- the web site: creates the users, sets their plan and records their payments
    def upsert_plan(s, name: str, body: PlanBody) -> Plan:  # noqa: ANN001
        plan = s.get(Plan, name)
        if plan is None:
            plan = Plan(name=name)
            s.add(plan)
        plan.monthly_hours = body.monthly_hours
        plan.max_concurrent = body.max_concurrent
        plan.requests_per_minute = body.requests_per_minute
        plan.priority = body.priority
        return plan

    @app.put("/v1/site/plans/{name}")
    def site_plan(_site: SiteDep, name: str, body: PlanBody) -> dict:
        with db.session() as s:
            return {"plan": plan_view(upsert_plan(s, name, body))}

    @app.put("/v1/admin/plans/{name}")
    def admin_plan(_admin: AdminDep, name: str, body: PlanBody) -> dict:
        with db.session() as s:
            return {"plan": plan_view(upsert_plan(s, name, body))}

    @app.put("/v1/site/users/{external_id}")
    def site_user(_site: SiteDep, external_id: str, body: SiteUser) -> dict:
        with db.session() as s:
            user = s.execute(select(User).where(User.external_id == external_id)).scalar_one_or_none()
            email = body.email.strip().lower() if body.email else None
            if email and s.execute(select(User).where(User.email == email, User.external_id != external_id)).first():
                raise HTTPException(409, "Another user has that e-mail")
            if body.plan is not None and s.get(Plan, body.plan) is None:
                raise HTTPException(422, f"No plan {body.plan}")
            if user is None:
                if not email or not body.plan:
                    raise HTTPException(422, "A new user needs an e-mail and a plan")
                user = User(external_id=external_id, email=email, plan_name=body.plan)
                s.add(user)
            if email:
                user.email = email
            if body.password is not None:
                user.password_hash = hash_password(body.password)
                # A new password closes the sessions of the devices.
                if user.id is not None:
                    s.execute(ApiToken.__table__.delete().where(ApiToken.user_id == user.id))
            if body.plan is not None:
                user.plan_name = body.plan
            if body.paid_until is not None:
                # Stored in UTC without time zone; a date without zone is taken as UTC.
                paid = body.paid_until
                user.paid_until = paid.astimezone(timezone.utc).replace(tzinfo=None) if paid.tzinfo else paid
            if body.enabled is not None:
                user.enabled = body.enabled
            s.flush()
            s.refresh(user)
            return {"user": user_view(user), "account": scheduler.account(s, user, cfg.grace_days).as_dict()}

    @app.get("/v1/site/users/{external_id}")
    def site_get_user(_site: SiteDep, external_id: str) -> dict:
        with db.session() as s:
            user = s.execute(select(User).where(User.external_id == external_id)).scalar_one_or_none()
            if user is None:
                raise HTTPException(404, "No such user")
            return {"user": user_view(user), "account": scheduler.account(s, user, cfg.grace_days).as_dict()}

    @app.get("/v1/admin/queue")
    def admin_queue(_admin: AdminDep) -> dict:
        with db.session() as s:
            rows = s.execute(
                select(Job.job_class, Job.status, func.count()).where(Job.status.in_(("queued", "running")))
                .group_by(Job.job_class, Job.status)
            ).all()
        queue: dict[str, dict[str, int]] = {c: {"queued": 0, "running": 0} for c in JOB_CLASSES}
        for job_class, status, count in rows:
            queue.setdefault(job_class, {"queued": 0, "running": 0})[status] = count
        return {"queue": queue}

    @app.get("/v1/admin/jobs")
    def admin_jobs(_admin: AdminDep, limit: int = 30) -> dict:
        """The jobs waiting and running, and the last ones that ended."""
        limit = max(1, min(limit, 200))
        with db.session() as s:
            active = s.execute(
                select(Job, User.email, Node.name).join(User, User.id == Job.user_id)
                .outerjoin(Node, Node.id == Job.node_id)
                .where(Job.status.in_(scheduler.ACTIVE)).order_by(Job.created_at).limit(200)
            ).all()
            ended = s.execute(
                select(Job, User.email, Node.name).join(User, User.id == Job.user_id)
                .outerjoin(Node, Node.id == Job.node_id)
                .where(Job.status.in_(scheduler.FINAL)).order_by(Job.finished_at.desc()).limit(limit)
            ).all()

            def view(row) -> dict:  # noqa: ANN001
                job, email, node = row
                return {**job_view(job), "user": email, "node": node}

            return {"active": [view(r) for r in active], "ended": [view(r) for r in ended]}

    # ---- the administration panel (a single page that uses the API above)
    @app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
    def admin_panel(_local: Annotated[None, Depends(local_only)]) -> HTMLResponse:
        page = resources.files("tisma_cloud").joinpath("static/admin.html").read_text(encoding="utf-8")
        # The page loads nothing from other sites and is not shown inside other pages.
        return HTMLResponse(page, headers={
            "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; "
                                       "frame-ancestors 'none'",
            "X-Frame-Options": "DENY", "Cache-Control": "no-store"})

    return app
