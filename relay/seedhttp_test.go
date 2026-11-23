package main

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/libp2p/go-libp2p/core/peer"
)

// bearer signs a ticket payload as the master does (peers.py: canonical JSON, sorted keys).
func bearer(t *testing.T, key ed25519.PrivateKey, payload map[string]any) string {
	t.Helper()
	data, _ := json.Marshal(payload)
	enc := base64.RawURLEncoding
	return enc.EncodeToString(data) + "." + enc.EncodeToString(ed25519.Sign(key, data))
}

func downloadTicket(t *testing.T, key ed25519.PrivateKey, account string, capMB int64) string {
	return bearer(t, key, map[string]any{"account": account, "cap_mb_per_hour": capMB,
		"expires": time.Now().Add(time.Hour).Unix(), "peer_id": ""})
}

func uploadTicket(t *testing.T, key ed25519.PrivateKey, account string, data []byte) string {
	sum := sha256.Sum256(data)
	return bearer(t, key, map[string]any{"account": account, "expires": time.Now().Add(24 * time.Hour).Unix(),
		"op": "upload", "sha256": hex.EncodeToString(sum[:]), "size": len(data)})
}

func call(t *testing.T, srv *httptest.Server, method, path, ticket string, body []byte) (int, http.Header, []byte) {
	t.Helper()
	req, _ := http.NewRequest(method, srv.URL+path, bytes.NewReader(body))
	if ticket != "" {
		req.Header.Set("Authorization", "Bearer "+ticket)
	}
	res, err := srv.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	data, _ := io.ReadAll(res.Body)
	return res.StatusCode, res.Header, data
}

// stand-in for clamscan: exit 1 and «FOUND» for a file with EICAR (the CI's relay job runs the real one).
func fakeClamAV(t *testing.T) string {
	if real := os.Getenv("TISMA_CLAMSCAN"); real != "" {
		return real
	}
	clam := filepath.Join(t.TempDir(), "clamscan")
	_ = os.WriteFile(clam, []byte("#!/bin/sh\nfor f; do :; done\nif unzip -l \"$f\" 2>/dev/null | grep -q EICAR || grep -q EICAR \"$f\"; then echo \"$f: Eicar-Signature FOUND\"; exit 1; fi\nexit 0\n"), 0o755)
	return clam
}

func seedServer(t *testing.T) (*Seeder, *SeedHTTP, *httptest.Server, ed25519.PrivateKey) {
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	s := &Seeder{Store: &Store{Dir: t.TempDir()}, ClamAV: fakeClamAV(t), servable: map[string]bool{},
		blocked: map[string]bool{}, accounts: map[string]bool{}, peers: map[string]bool{}}
	h := &SeedHTTP{S: s, Key: pub}
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	return s, h, srv, priv
}

func noisyProject(t *testing.T, bytesOfNoise int) []byte {
	noise := make([]byte, bytesOfNoise)
	_, _ = rand.Read(noise)
	return zipOf(t, map[string]string{"3D/3dmodel.model": hex.EncodeToString(noise)})
}

func TestTheSeederServesPiecesOverHTTPSWithATicket(t *testing.T) {
	s, _, srv, priv := seedServer(t)
	data := noisyProject(t, 1500000) // two pieces
	sha, _ := put(t, s.Store, data)
	s.servable[sha] = true
	ticket := downloadTicket(t, priv, "ua_1", 100)

	if code, _, _ := call(t, srv, "GET", "/v1/seed/"+sha+"/pieces", "", nil); code != 401 {
		t.Fatalf("without a ticket: %d", code)
	}
	code, _, body := call(t, srv, "GET", "/v1/seed/"+sha+"/pieces", ticket, nil)
	var list struct {
		OK     bool     `json:"ok"`
		Size   int64    `json:"size"`
		Hashes []string `json:"hashes"`
	}
	if code != 200 || json.Unmarshal(body, &list) != nil || !list.OK || list.Size != int64(len(data)) || len(list.Hashes) < 2 {
		t.Fatalf("pieces: %d %s", code, body)
	}
	var whole []byte
	for n := range list.Hashes {
		code, _, piece := call(t, srv, "GET", fmt.Sprintf("/v1/seed/%s/piece/%d", sha, n), ticket, nil)
		got := sha256.Sum256(piece)
		if code != 200 || hex.EncodeToString(got[:]) != list.Hashes[n] {
			t.Fatalf("piece %d: %d", n, code)
		}
		whole = append(whole, piece...)
	}
	if !bytes.Equal(whole, data) {
		t.Fatal("the pieces do not make the file")
	}
	if code, _, _ := call(t, srv, "GET", fmt.Sprintf("/v1/seed/%s/piece/%d", sha, len(list.Hashes)), ticket, nil); code != 404 {
		t.Fatalf("a piece out of the file: %d", code)
	}
	other := hex.EncodeToString(make([]byte, 32))
	if code, _, _ := call(t, srv, "GET", "/v1/seed/"+other+"/pieces", ticket, nil); code != 404 {
		t.Fatalf("a file it does not have: %d", code)
	}
	// An upload ticket does not download.
	if code, _, _ := call(t, srv, "GET", "/v1/seed/"+sha+"/pieces", uploadTicket(t, priv, "ua_1", data), nil); code != 401 {
		t.Fatalf("an upload ticket downloaded: %d", code)
	}
	// Blocked: the account (403), the file (451).
	s.accounts["ua_1"] = true
	if code, _, _ := call(t, srv, "GET", "/v1/seed/"+sha+"/pieces", ticket, nil); code != 403 {
		t.Fatalf("a blocked account: %d", code)
	}
	delete(s.accounts, "ua_1")
	s.blocked[sha] = true
	if code, _, _ := call(t, srv, "GET", "/v1/seed/"+sha+"/pieces", ticket, nil); code != 451 {
		t.Fatalf("a blocked file: %d", code)
	}
}

func TestAnAccountOverItsCapWaitsForTheNextHourOverHTTPS(t *testing.T) {
	s, h, srv, priv := seedServer(t)
	now := time.Date(2026, 11, 16, 10, 59, 0, 0, time.UTC)
	h.now = func() time.Time { return now }
	sha, _ := put(t, s.Store, noisyProject(t, 2500000)) // three pieces
	s.servable[sha] = true
	ticket := downloadTicket(t, priv, "ua_2", 1) // 1 MB per hour
	if code, _, _ := call(t, srv, "GET", "/v1/seed/"+sha+"/piece/0", ticket, nil); code != 200 {
		t.Fatalf("first piece: %d", code)
	}
	code, header, _ := call(t, srv, "GET", "/v1/seed/"+sha+"/piece/1", ticket, nil)
	if code != 429 || header.Get("Retry-After") == "" {
		t.Fatalf("over the cap: %d", code)
	}
	if wait, _ := strconv.Atoi(header.Get("Retry-After")); wait < 1 || wait > 61 {
		t.Fatalf("Retry-After %d, not until the next hour", wait)
	}
	now = now.Add(2 * time.Minute)
	if code, _, _ := call(t, srv, "GET", "/v1/seed/"+sha+"/piece/1", ticket, nil); code != 200 {
		t.Fatalf("the next hour: %d", code)
	}
}

func uploadAll(t *testing.T, srv *httptest.Server, ticket string, data []byte) (int, []byte) {
	sum := sha256.Sum256(data)
	sha := hex.EncodeToString(sum[:])
	pieces := (len(data) + PieceSize - 1) / PieceSize
	var code int
	var body []byte
	for n := 0; n < pieces; n++ {
		end := min((n+1)*PieceSize, len(data))
		code, _, body = call(t, srv, "PUT", fmt.Sprintf("/v1/seed/upload/%s/%d", sha, n), ticket, data[n*PieceSize:end])
		if n < pieces-1 && code != 200 {
			t.Fatalf("piece %d: %d %s", n, code, body)
		}
	}
	return code, body
}

func TestAnAuthorUploadsANewModelInPiecesAndItIsServed(t *testing.T) {
	s, _, srv, priv := seedServer(t)
	data := noisyProject(t, 2500000) // three pieces
	sum := sha256.Sum256(data)
	sha := hex.EncodeToString(sum[:])
	ticket := uploadTicket(t, priv, "ua_3", data)
	base := "/v1/seed/upload/" + sha

	if code, _, _ := call(t, srv, "GET", base, downloadTicket(t, priv, "ua_3", 100), nil); code != 401 {
		t.Fatalf("a download ticket uploads: %d", code)
	}
	if code, _, _ := call(t, srv, "GET", "/v1/seed/upload/"+hex.EncodeToString(make([]byte, 32)), ticket, nil); code != 401 {
		t.Fatalf("a ticket of another file: %d", code)
	}
	code, _, body := call(t, srv, "GET", base, ticket, nil)
	if code != 200 || !bytes.Contains(body, []byte(`"have":[]`)) || !bytes.Contains(body, []byte(`"pieces":3`)) {
		t.Fatalf("state before: %d %s", code, body)
	}
	// Piece 1 first (an upload cut and resumed), a wrong length, a piece out of range, too large.
	if code, _, _ := call(t, srv, "PUT", base+"/1", ticket, data[PieceSize:2*PieceSize]); code != 200 {
		t.Fatalf("piece 1: %d", code)
	}
	if code, _, _ := call(t, srv, "PUT", base+"/1", ticket, data[PieceSize:2*PieceSize]); code != 200 {
		t.Fatalf("piece 1 again: %d", code)
	}
	if code, _, _ := call(t, srv, "PUT", base+"/0", ticket, data[:10]); code != 409 {
		t.Fatalf("a short piece: %d", code)
	}
	if code, _, _ := call(t, srv, "PUT", base+"/3", ticket, data[:10]); code != 409 {
		t.Fatalf("a piece out of range: %d", code)
	}
	if code, _, _ := call(t, srv, "PUT", base+"/2", ticket, data[:PieceSize]); code != 413 {
		t.Fatalf("a last piece larger than the size: %d", code)
	}
	code, _, body = call(t, srv, "GET", base, ticket, nil)
	if code != 200 || !bytes.Contains(body, []byte(`"have":[1]`)) {
		t.Fatalf("state to resume: %d %s", code, body)
	}
	code, body = uploadAll(t, srv, ticket, data)
	if code != 201 || !bytes.Contains(body, []byte(`"complete":true`)) {
		t.Fatalf("the last piece: %d %s", code, body)
	}
	if !s.Store.Has(sha) {
		t.Fatal("not kept")
	}
	// Served at once, before the index lists its model.
	if code, _, _ := call(t, srv, "GET", "/v1/seed/"+sha+"/pieces", downloadTicket(t, priv, "ua_9", 100), nil); code != 200 {
		t.Fatalf("download after the upload: %d", code)
	}
	// Uploaded again: nothing to do.
	if code, _, _ := call(t, srv, "PUT", base+"/0", ticket, data[:PieceSize]); code != 200 {
		t.Fatalf("a file it has: %d", code)
	}
}

func TestAnUploadThatIsNotTheTicketsOrNotSafeIsDropped(t *testing.T) {
	s, _, srv, priv := seedServer(t)
	data := noisyProject(t, 600000)
	// Another file with the size of the ticket: its SHA-256 differs.
	other := append([]byte{}, data...)
	other[len(other)/2] ^= 0xff
	ticket := uploadTicket(t, priv, "ua_4", data)
	sum := sha256.Sum256(data)
	sha := hex.EncodeToString(sum[:])
	for n := 0; n*PieceSize < len(other); n++ {
		end := min((n+1)*PieceSize, len(other))
		code, _, body := call(t, srv, "PUT", fmt.Sprintf("/v1/seed/upload/%s/%d", sha, n), ticket, other[n*PieceSize:end])
		if end == len(other) && (code != 422 || !bytes.Contains(body, []byte("SHA-256"))) {
			t.Fatalf("another file: %d %s", code, body)
		}
	}
	if s.Store.Has(sha) {
		t.Fatal("kept")
	}
	if left, _ := filepath.Glob(filepath.Join(s.Store.Dir, "uploads", sha+"*")); len(left) != 0 {
		t.Fatal("its pieces were not deleted")
	}
	// The antivirus finds something (3 octies): refused and deleted.
	infected := zipOf(t, map[string]string{"3D/3dmodel.model": "<model/>", "EICAR.txt": eicar})
	code, body := uploadAll(t, srv, uploadTicket(t, priv, "ua_4", infected), infected)
	if code != 422 || !bytes.Contains(body, []byte("antivirus")) || bytes.Contains(body, []byte(s.Store.Dir)) {
		t.Fatalf("an infected file: %d %s", code, body)
	}
	// Not a project at all.
	junk := bytes.Repeat([]byte("MZ"), 1000)
	if code, _ := uploadAll(t, srv, uploadTicket(t, priv, "ua_4", junk), junk); code != 422 {
		t.Fatalf("a file of another kind: %d", code)
	}
}

func TestUploadsStopAtTheRoomAndTheHourlyCap(t *testing.T) {
	s, h, srv, priv := seedServer(t)
	data := noisyProject(t, 2500000)
	sum := sha256.Sum256(data)
	base := "/v1/seed/upload/" + hex.EncodeToString(sum[:])
	ticket := uploadTicket(t, priv, "ua_5", data)
	s.MaxBytes = int64(len(data)) / 2
	if code, _, _ := call(t, srv, "PUT", base+"/0", ticket, data[:PieceSize]); code != 507 {
		t.Fatalf("no room: %d", code)
	}
	s.MaxBytes = 0
	h.UploadBytes = PieceSize // one piece per hour
	if code, _, _ := call(t, srv, "PUT", base+"/0", ticket, data[:PieceSize]); code != 200 {
		t.Fatalf("first piece: %d", code)
	}
	code, header, _ := call(t, srv, "PUT", base+"/1", ticket, data[PieceSize:2*PieceSize])
	if code != 429 || header.Get("Retry-After") == "" {
		t.Fatalf("over the upload cap: %d", code)
	}
	s.accounts["ua_5"] = true
	if code, _, _ := call(t, srv, "PUT", base+"/1", ticket, data[PieceSize:2*PieceSize]); code != 403 {
		t.Fatalf("a blocked account: %d", code)
	}
}

func TestACycleKeepsAFileUploadedBeforeTheIndexListsIt(t *testing.T) {
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	m := &fakeMaster{key: priv}
	m.index, _ = json.Marshal(map[string]any{"models": []any{}})
	m.block, _ = json.Marshal(map[string]any{"files": []any{}, "models": []any{}, "accounts": []any{}, "peers": []any{}})
	srv := httptest.NewServer(m.handler(t))
	defer srv.Close()
	s := &Seeder{Master: &Master{URL: srv.URL, Token: "seed-token", Key: pub, HTTP: srv.Client()},
		Store: &Store{Dir: t.TempDir()}}
	sha, _ := put(t, s.Store, noisyProject(t, 1000))
	stale, _ := put(t, s.Store, noisyProject(t, 1000))
	s.Uploaded(sha)
	if _, err := s.Cycle(t.Context()); err != nil {
		t.Fatal(err)
	}
	if !s.Store.Has(sha) {
		t.Fatal("a file uploaded a moment ago was deleted")
	}
	if s.Store.Has(stale) {
		t.Fatal("a file of no model was kept")
	}
	if ok, _ := s.Servable(sha); !ok {
		t.Fatal("not served")
	}
}

// Tickets made by the master's code (peers.ticket without a peer key, peers.upload_ticket) with a fixed key: the
// seeder reads them, and neither one opens the relay nor serves the other use.
func TestTicketsOfTheMastersCodeForHTTPS(t *testing.T) {
	lib, _ := hex.DecodeString("ea4a6c63e29c520abef5507b132ec5f9954776aebebe7b92421eea691446d22c")
	down := "eyJhY2NvdW50IjoidWFfdGVzdCIsImNhcF9tYl9wZXJfaG91ciI6MjAwLCJleHBpcmVzIjo0MTAyNDQ0ODAwLCJwZWVyX2lkIjoiIn0." +
		"oQVkZ4gQrsEoEeFsKEYxSgyHcYyACEeJa4NcPk01C_ezxVNWKESLd0MEptIE_z-Qv183OcJaocyVZZoXoQ7NCQ"
	up := "eyJhY2NvdW50IjoidWFfdGVzdCIsImV4cGlyZXMiOjQxMDI0NDQ4MDAsIm9wIjoidXBsb2FkIiwic2hhMjU2IjoiYWJhYmFiYWJhYmFiYWJh" +
		"YmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYiIsInNpemUiOjMxNDU3Mjh9." +
		"6otA5gy-6QT88GQIbgAPGBxKYt4PxoXTzXAtu7bfq7t7o29IBdsdtdjpoUgvgzDNbFtyFGIMx2jrBu17w64YBw"
	now := time.Unix(4102444800-60, 0)
	got, err := ParseBearer(down, lib, "", now)
	if err != nil || got.Account != "ua_test" || got.CapMBPerHour != 200 || got.PeerID != "" {
		t.Fatalf("download ticket: %+v %v", got, err)
	}
	got, err = ParseBearer(up, lib, "upload", now)
	if err != nil || got.SHA256 != strings.Repeat("ab", 32) || got.Size != 3145728 {
		t.Fatalf("upload ticket: %+v %v", got, err)
	}
	if _, err := ParseBearer(up, lib, "", now); err == nil {
		t.Fatal("an upload ticket downloads")
	}
	if _, err := ParseBearer(down, lib, "upload", now); err == nil {
		t.Fatal("a download ticket uploads")
	}
	if _, err := ParseTicket(down, lib, peer.ID("12D3KooWA"), now); err == nil {
		t.Fatal("a ticket without a peer opens the relay")
	}
	if _, err := ParseTicket(up, lib, peer.ID("12D3KooWA"), now); err == nil {
		t.Fatal("an upload ticket opens the relay")
	}
	if _, err := ParseBearer(down, lib, "", time.Unix(4102444800, 0)); err == nil {
		t.Fatal("an ended ticket")
	}
}

// Two accounts with an upload ticket of one file (anyone may publish a model that lists a file already in the index):
// pieces sent by one never stand in for the other's, so nobody can spoil another author's upload with junk.
func TestAnotherAccountCannotSpoilAnUpload(t *testing.T) {
	s, _, srv, priv := seedServer(t)
	data := noisyProject(t, 2500000) // three pieces
	sum := sha256.Sum256(data)
	sha := hex.EncodeToString(sum[:])
	base := "/v1/seed/upload/" + sha
	author, other := uploadTicket(t, priv, "ua_author", data), uploadTicket(t, priv, "ua_other", data)
	if code, _, _ := call(t, srv, "PUT", base+"/0", author, data[:PieceSize]); code != 200 {
		t.Fatalf("the author's first piece: %d", code)
	}
	junk := make([]byte, len(data)-2*PieceSize)
	if code, _, _ := call(t, srv, "PUT", base+"/2", other, junk); code != 200 {
		t.Fatalf("the other account's piece: %d", code)
	}
	code, _, body := call(t, srv, "GET", base, author, nil)
	if code != 200 || !bytes.Contains(body, []byte(`"have":[0]`)) {
		t.Fatalf("the author's state: %d %s", code, body)
	}
	code, body = uploadAll(t, srv, author, data)
	if code != 201 || !s.Store.Has(sha) {
		t.Fatalf("the author's upload: %d %s", code, body)
	}
}
