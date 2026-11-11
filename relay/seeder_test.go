package main

import (
	"archive/zip"
	"bufio"
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/libp2p/go-libp2p"
	"github.com/libp2p/go-libp2p/core/host"
	"github.com/libp2p/go-libp2p/core/network"
	"github.com/libp2p/go-libp2p/core/peer"
)

type openPolicy struct {
	mu      sync.Mutex
	blocked map[string]bool
	cap     int64
	sent    int64
}

func (p *openPolicy) Servable(sha string) (bool, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.blocked[sha] {
		return false, true
	}
	return true, false
}
func (p *openPolicy) Busy() bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.cap > 0 && p.sent >= p.cap
}
func (p *openPolicy) Sent(n int) { p.mu.Lock(); p.sent += int64(n); p.mu.Unlock() }

func plainHost(t *testing.T) host.Host {
	h, err := libp2p.New(libp2p.ListenAddrStrings("/ip4/127.0.0.1/tcp/0"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { h.Close() })
	return h
}

// put keeps data in a store and returns its SHA-256 and piece root.
func put(t *testing.T, store *Store, data []byte) (string, string) {
	t.Helper()
	sum := sha256.Sum256(data)
	sha := hex.EncodeToString(sum[:])
	if err := os.WriteFile(filepath.Join(store.Dir, sha), data, 0o644); err != nil {
		t.Fatal(err)
	}
	_, hashes, err := store.Hashes(sha)
	if err != nil {
		t.Fatal(err)
	}
	return sha, PieceRoot(hashes)
}

func connect(t *testing.T, a, b host.Host) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := a.Connect(ctx, peer.AddrInfo{ID: b.ID(), Addrs: b.Addrs()}); err != nil {
		t.Fatal(err)
	}
}

func TestPiecesAreCheckedFromAnyPeer(t *testing.T) {
	server, client := plainHost(t), plainHost(t)
	src, dst := &Store{Dir: t.TempDir()}, &Store{Dir: t.TempDir()}
	policy := &openPolicy{blocked: map[string]bool{}}
	ServeLibrary(server, src, policy)
	data := bytes.Repeat([]byte("tisma "), (2*PieceSize+1234)/6)
	sha, root := put(t, src, data)
	connect(t, client, server)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := Fetch(ctx, client, server.ID(), dst, sha, root); err != nil {
		t.Fatalf("fetch: %v", err)
	}
	got, _ := os.ReadFile(filepath.Join(dst.Dir, sha))
	if !bytes.Equal(got, data) {
		t.Fatal("the file is not the same")
	}
	// P2P10: a list that does not give piece_roots is dropped.
	other := &Store{Dir: t.TempDir()}
	if err := Fetch(ctx, client, server.ID(), other, sha, strings.Repeat("0", 64)); !errors.Is(err, ErrBadList) {
		t.Fatalf("a wrong piece list: %v", err)
	}
	// P2P11: a blocked file answers «blocked» and nothing is sent.
	policy.blocked[sha] = true
	if err := Fetch(ctx, client, server.ID(), other, sha, root); err == nil || !strings.Contains(err.Error(), "blocked") {
		t.Fatalf("a blocked file: %v", err)
	}
	if entries, _ := os.ReadDir(other.Dir); len(entries) != 0 {
		t.Fatal("something of a refused file was kept")
	}
	// The upload cap: «busy».
	delete(policy.blocked, sha)
	policy.cap = 1
	if err := Fetch(ctx, client, server.ID(), other, sha, root); err == nil || !strings.Contains(err.Error(), "busy") {
		t.Fatalf("over the upload cap: %v", err)
	}
}

func TestAnAlteredPieceIsDropped(t *testing.T) {
	// P2P2: a peer that sends a good list and a wrong piece.
	liar, client := plainHost(t), plainHost(t)
	data := bytes.Repeat([]byte("x"), PieceSize+10)
	store := &Store{Dir: t.TempDir()}
	sha, root := put(t, store, data)
	_, hashes, _ := store.Hashes(sha)
	liar.SetStreamHandler(LibraryProtocol, func(s network.Stream) {
		defer s.Close()
		r := bufio.NewReader(s)
		for {
			line, err := readLine(r)
			if err != nil {
				return
			}
			var req request
			_ = json.Unmarshal(line, &req)
			if req.Op == "pieces" {
				list := []string{hex.EncodeToString(hashes[0]), hex.EncodeToString(hashes[1])}
				_ = answer(s, map[string]any{"ok": true, "size": len(data), "hashes": list})
				continue
			}
			_ = answer(s, map[string]any{"ok": true, "length": PieceSize})
			_, _ = s.Write(bytes.Repeat([]byte("y"), PieceSize))
		}
	})
	connect(t, client, liar)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	out := &Store{Dir: t.TempDir()}
	if err := Fetch(ctx, client, liar.ID(), out, sha, root); !errors.Is(err, ErrBadPiece) {
		t.Fatalf("an altered piece: %v", err)
	}
	if entries, _ := os.ReadDir(out.Dir); len(entries) != 0 {
		t.Fatal("the altered file was kept")
	}
}

func zipOf(t *testing.T, files map[string]string) []byte {
	var buf bytes.Buffer
	z := zip.NewWriter(&buf)
	for name, body := range files {
		w, err := z.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		_, _ = w.Write([]byte(body))
	}
	_ = z.Close()
	return buf.Bytes()
}

func TestSafeFiles(t *testing.T) {
	dir := t.TempDir()
	write := func(name string, data []byte) string {
		p := filepath.Join(dir, name)
		_ = os.WriteFile(p, data, 0o644)
		return p
	}
	good := map[string][]byte{
		"pieza.3mf": zipOf(t, map[string]string{"3D/3dmodel.model": "<model/>", "Metadata/tisma_project.json": "{}"}),
		"foto.png":  []byte("\x89PNG\r\n\x1a\n...."),
		"pieza.stl": []byte("solid pieza\nendsolid\n"),
		"receta":    []byte(`{"layers":{"height":0.2}}`),
	}
	for name, data := range good {
		if err := SafeFile(write(name, data)); err != nil {
			t.Errorf("%s: %v", name, err)
		}
	}
	bad := map[string][]byte{
		"out.3mf":     zipOf(t, map[string]string{"../../evil.txt": "x"}),
		"program.3mf": zipOf(t, map[string]string{"3D/run.exe": "MZ"}),
		"gcode.3mf":   zipOf(t, map[string]string{"Metadata/plate_1.gcode": "G1"}),
		"a.exe":       append([]byte("MZ"), make([]byte, 200)...),
		"script":      []byte("#!/bin/sh\nrm -rf /\n"),
		"short":       []byte("hola"),
	}
	for name, data := range bad {
		if err := SafeFile(write(name, data)); err == nil {
			t.Errorf("%s was accepted", name)
		}
	}
}

// fakeMaster serves a signed index and blocklist, the peers of a model, and keeps what the seeder tells it.
type fakeMaster struct {
	key      ed25519.PrivateKey
	index    []byte
	block    []byte
	peers    []map[string]any
	mu       sync.Mutex
	checks   []map[string]any
	announce []map[string]any
	bound    string
}

func (m *fakeMaster) handler(t *testing.T) http.Handler {
	mux := http.NewServeMux()
	serve := func(data func() []byte, signed bool) http.HandlerFunc {
		return func(w http.ResponseWriter, _ *http.Request) {
			if signed {
				_, _ = w.Write(ed25519.Sign(m.key, data()))
				return
			}
			_, _ = w.Write(data())
		}
	}
	mux.Handle("/v1/library/index.json", serve(func() []byte { return m.index }, false))
	mux.Handle("/v1/library/index.json.sig", serve(func() []byte { return m.index }, true))
	mux.Handle("/v1/library/blocklist.json", serve(func() []byte { return m.block }, false))
	mux.Handle("/v1/library/blocklist.json.sig", serve(func() []byte { return m.block }, true))
	mux.HandleFunc("/v1/library/peer-key", func(w http.ResponseWriter, r *http.Request) {
		var b map[string]string
		_ = json.NewDecoder(r.Body).Decode(&b)
		m.bound = b["public_key"]
		_, _ = w.Write([]byte("{}"))
	})
	mux.HandleFunc("/v1/library/peers", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"peers": m.peers})
	})
	mux.HandleFunc("/v1/library/peers/announce", func(w http.ResponseWriter, r *http.Request) {
		var b map[string]any
		_ = json.NewDecoder(r.Body).Decode(&b)
		m.mu.Lock()
		m.announce = append(m.announce, b)
		m.mu.Unlock()
		_, _ = w.Write([]byte("{}"))
	})
	mux.HandleFunc("/v1/library/models/", func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer seed-token" {
			w.WriteHeader(401)
			return
		}
		var b map[string]any
		_ = json.NewDecoder(r.Body).Decode(&b)
		b["path"] = r.URL.Path
		m.mu.Lock()
		m.checks = append(m.checks, b)
		m.mu.Unlock()
		_, _ = w.Write([]byte("{}"))
	})
	return mux
}

func TestTheSeederKeepsChecksAndAnnouncesTheLibrary(t *testing.T) {
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	// The author's app, with two models' files.
	author := plainHost(t)
	authorStore := &Store{Dir: t.TempDir()}
	ServeLibrary(author, authorStore, &openPolicy{blocked: map[string]bool{}})
	noise := make([]byte, 600000) // more than a piece once zipped, and not a zip bomb
	_, _ = rand.Read(noise)
	clean := zipOf(t, map[string]string{"3D/3dmodel.model": hex.EncodeToString(noise)})
	infected := zipOf(t, map[string]string{"3D/3dmodel.model": "<model/>", "EICAR.txt": "X5O!P%@AP"})
	shaClean, rootClean := put(t, authorStore, clean)
	shaBad, rootBad := put(t, authorStore, infected)
	model := func(id string, sha, root string, size int, published string) map[string]any {
		return map[string]any{"id": id, "revision": 1, "published": published, "size": size,
			"files":  []map[string]any{{"path": "pieza.3mf", "size": size, "sha256": sha, "role": "project"}},
			"author": map[string]any{"account": "ua_1"}, "peer_key": "k", "checked": false,
			"swarm": map[string]any{"piece_roots": map[string]string{sha: root}}}
	}
	m := &fakeMaster{key: priv, peers: []map[string]any{{"peer_id": author.ID().String(),
		"addrs": []string{author.Addrs()[0].String()}}}}
	m.index, _ = json.Marshal(map[string]any{"models": []any{
		model("lm_clean", shaClean, rootClean, len(clean), "2026-11-11T10:00:00Z"),
		model("lm_bad", shaBad, rootBad, len(infected), "2026-11-11T09:00:00Z")}})
	m.block, _ = json.Marshal(map[string]any{"files": []any{}, "models": []any{}, "accounts": []any{}, "peers": []any{}})
	srv := httptest.NewServer(m.handler(t))
	defer srv.Close()
	// ClamAV, as a program: exit 1 and «FOUND» for the file with EICAR.
	clam := filepath.Join(t.TempDir(), "clamscan")
	_ = os.WriteFile(clam, []byte("#!/bin/sh\nfor f; do :; done\nif unzip -l \"$f\" 2>/dev/null | grep -q EICAR || grep -q EICAR \"$f\"; then echo \"$f: Eicar-Signature FOUND\"; exit 1; fi\nexit 0\n"), 0o755)

	key := mustKey(t)
	seed, err := libp2p.New(libp2p.Identity(key), libp2p.ListenAddrStrings("/ip4/127.0.0.1/tcp/0"))
	if err != nil {
		t.Fatal(err)
	}
	defer seed.Close()
	peerKey, _ := peerKeyOf(key)
	s := &Seeder{Master: &Master{URL: srv.URL, Token: "seed-token", Key: pub, HTTP: srv.Client()}, Host: seed,
		PeerKey: peerKey, Sign: key.Sign, Store: &Store{Dir: t.TempDir()}, ClamAV: clam,
		Announce: []string{"/ip4/200.40.10.7/tcp/4002"}}
	ServeLibrary(seed, s.Store, s)
	if err := s.Bind(); err != nil || m.bound != peerKey {
		t.Fatalf("bind: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	whole, err := s.Cycle(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(whole) != 2 || !s.Store.Has(shaClean) {
		t.Fatalf("seeding %v", whole)
	}
	verdict := map[string]bool{}
	for _, c := range m.checks {
		verdict[c["path"].(string)] = c["clean"].(bool)
	}
	if !verdict["/v1/library/models/lm_clean/check"] || verdict["/v1/library/models/lm_bad/check"] {
		t.Fatalf("checks told: %v", m.checks)
	}
	if s.Store.Has(shaBad) {
		t.Fatal("the infected file is still kept")
	}
	if ok, blocked := s.Servable(shaBad); ok || !blocked {
		t.Fatal("the infected file is still served")
	}
	// The announcement is signed with the seeder's peer key over what peers.announcement_bytes signs.
	a := m.announce[len(m.announce)-1]
	sig, _ := base64.StdEncoding.DecodeString(a["signature"].(string))
	delete(a, "signature")
	signedBytes, _ := json.Marshal(a)
	raw, _ := base64.StdEncoding.DecodeString(peerKey)
	if !ed25519.Verify(raw, signedBytes, sig) || a["direct"] != true {
		t.Fatalf("announcement %v not signed as the master checks it", a)
	}
	// A second cycle tells nothing again; a model that leaves the index is deleted (P2P3).
	n := len(m.checks)
	m.index, _ = json.Marshal(map[string]any{"models": []any{}})
	if _, err := s.Cycle(ctx); err != nil {
		t.Fatal(err)
	}
	if len(m.checks) != n || s.Store.Has(shaClean) {
		t.Fatal("told again, or kept a model that left the index")
	}
}
