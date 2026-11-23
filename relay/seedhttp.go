// The seeder over HTTPS (p2p_library.md 8 octies and 8 nonies), while the apps and the engine do not speak the peer
// network yet: the pieces of the library's files downloaded with a ticket of 8 sexies, and the files of a new model
// uploaded by its author with an upload ticket of the master, in pieces of 1 MiB that can be resumed. It listens on
// the local machine; the tunnel of the master's public address sends /v1/seed/ to it (OPERACION.md).
package main

import (
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// SeedHTTP serves the seeder's files and receives the authors' uploads.
type SeedHTTP struct {
	S           *Seeder
	Key         ed25519.PublicKey // the library key, checked with its certificate
	UploadBytes int64             // upload per account per hour (0: no limit)
	now         func() time.Time

	mu     sync.Mutex
	window time.Time
	down   map[string]int64 // bytes downloaded this hour, by account
	up     map[string]int64 // bytes uploaded this hour, by account
	busy   map[string]bool  // uploads being checked (one at a time per file and account)
}

func (h *SeedHTTP) clock() time.Time {
	if h.now != nil {
		return h.now()
	}
	return time.Now()
}

func jsonAnswer(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func refuseHTTP(w http.ResponseWriter, code int, detail string) {
	jsonAnswer(w, code, map[string]any{"ok": false, "detail": detail})
}

// count adds n bytes of an account this hour unless it passes its cap; it returns the seconds until the next hour
// when it would.
func (h *SeedHTTP) count(counts *map[string]int64, account string, n, capBytes int64) (bool, int) {
	h.mu.Lock()
	defer h.mu.Unlock()
	now := h.clock()
	if hour := now.Truncate(time.Hour); h.window.Before(hour) {
		h.window, h.down, h.up = hour, map[string]int64{}, map[string]int64{}
	}
	if *counts == nil {
		*counts = map[string]int64{}
	}
	if capBytes > 0 && (*counts)[account]+n > capBytes {
		return false, int(h.window.Add(time.Hour).Sub(now).Seconds()) + 1
	}
	(*counts)[account] += n
	return true, 0
}

func (h *SeedHTTP) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	parts := strings.Split(strings.Trim(r.URL.Path, "/"), "/")
	if len(parts) < 4 || parts[0] != "v1" || parts[1] != "seed" {
		refuseHTTP(w, http.StatusNotFound, "Not Found")
		return
	}
	bearer := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
	switch {
	case parts[2] == "upload" && len(parts) == 4 && r.Method == http.MethodGet:
		h.uploadState(w, bearer, parts[3])
	case parts[2] == "upload" && len(parts) == 5 && r.Method == http.MethodPut:
		h.uploadPiece(w, r, bearer, parts[3], parts[4])
	case len(parts) == 4 && parts[3] == "pieces" && r.Method == http.MethodGet:
		h.download(w, bearer, parts[2], -1)
	case len(parts) == 5 && parts[3] == "piece" && r.Method == http.MethodGet:
		n, err := strconv.Atoi(parts[4])
		if err != nil || n < 0 {
			refuseHTTP(w, http.StatusNotFound, "No such piece")
			return
		}
		h.download(w, bearer, parts[2], n)
	default:
		refuseHTTP(w, http.StatusNotFound, "Not Found")
	}
}

// ---- downloads (8 octies)

func (h *SeedHTTP) download(w http.ResponseWriter, bearer, sha string, piece int) {
	t, err := ParseBearer(bearer, h.Key, "", h.clock())
	if err != nil {
		refuseHTTP(w, http.StatusUnauthorized, "A ticket of POST /v1/library/relay-ticket is needed: "+err.Error())
		return
	}
	if h.S.BlockedAccount(t.Account, t.PeerID) {
		refuseHTTP(w, http.StatusForbidden, "Blocked in the library")
		return
	}
	if !sha256Hex.MatchString(sha) {
		refuseHTTP(w, http.StatusNotFound, "Not in the library")
		return
	}
	ok, blocked := h.S.Servable(sha)
	if blocked {
		refuseHTTP(w, http.StatusUnavailableForLegalReasons, "This file is in the library's blocklist")
		return
	}
	if !ok || !h.S.Store.Has(sha) {
		refuseHTTP(w, http.StatusNotFound, "This model is not available to download yet")
		return
	}
	size, hashes, err := h.S.Store.Hashes(sha)
	if err != nil {
		refuseHTTP(w, http.StatusNotFound, "This model is not available to download yet")
		return
	}
	if piece < 0 {
		list := make([]string, len(hashes))
		for i, x := range hashes {
			list[i] = hex.EncodeToString(x)
		}
		jsonAnswer(w, http.StatusOK, map[string]any{"ok": true, "size": size, "hashes": list})
		return
	}
	if piece >= len(hashes) {
		refuseHTTP(w, http.StatusNotFound, "No such piece")
		return
	}
	data, err := readPiece(h.S.Store.path(sha), piece)
	if err != nil {
		refuseHTTP(w, http.StatusNotFound, "No such piece")
		return
	}
	if allowed, wait := h.count(&h.down, t.Account, int64(len(data)), t.CapMBPerHour<<20); !allowed {
		w.Header().Set("Retry-After", strconv.Itoa(wait))
		refuseHTTP(w, http.StatusTooManyRequests, "The library is busy for this account: it goes on by itself next hour")
		return
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("Content-Length", strconv.Itoa(len(data)))
	w.WriteHeader(http.StatusOK)
	n, _ := w.Write(data)
	h.S.Sent(n)
}

// ---- uploads (8 nonies)

// upload is a file being uploaded: <dir>/uploads/<sha256>-<account>.part with its bytes at their places, and .have
// with one byte per piece (1: received), so that an upload cut by a restart goes on. Each account has its own: any
// account may get an upload ticket of a file another author is uploading (a model that lists it), and its pieces must
// not stand in for the author's.
type upload struct {
	part, have string
	size       int64
	pieces     int
}

func (h *SeedHTTP) uploadOf(t *Ticket) upload {
	dir := filepath.Join(h.S.Store.Dir, "uploads")
	account := sha256.Sum256([]byte(t.Account))
	name := t.SHA256 + "-" + hex.EncodeToString(account[:8])
	return upload{part: filepath.Join(dir, name+".part"), have: filepath.Join(dir, name+".have"),
		size: t.Size, pieces: int((t.Size + PieceSize - 1) / PieceSize)}
}

func (u upload) received() []byte {
	have, _ := os.ReadFile(u.have)
	if len(have) != u.pieces {
		have = make([]byte, u.pieces)
	}
	return have
}

// uploadTicket checks the upload ticket against the file of the address.
func (h *SeedHTTP) uploadTicket(w http.ResponseWriter, bearer, sha string) *Ticket {
	t, err := ParseBearer(bearer, h.Key, "upload", h.clock())
	if err != nil || t.SHA256 != sha {
		why := "an upload ticket of this file is needed (POST /v1/library/models/<id>/uploads)"
		if err != nil {
			why += ": " + err.Error()
		}
		refuseHTTP(w, http.StatusUnauthorized, why)
		return nil
	}
	if h.S.BlockedAccount(t.Account, "") {
		refuseHTTP(w, http.StatusForbidden, "Blocked in the library")
		return nil
	}
	if _, blocked := h.S.Servable(sha); blocked {
		refuseHTTP(w, http.StatusUnavailableForLegalReasons, "This file is in the library's blocklist")
		return nil
	}
	return t
}

func (h *SeedHTTP) uploadState(w http.ResponseWriter, bearer, sha string) {
	t := h.uploadTicket(w, bearer, sha)
	if t == nil {
		return
	}
	u := h.uploadOf(t)
	if h.S.Store.Has(sha) {
		all := make([]int, u.pieces)
		for i := range all {
			all[i] = i
		}
		jsonAnswer(w, http.StatusOK, map[string]any{"size": u.size, "pieces": u.pieces, "have": all, "complete": true})
		return
	}
	have := []int{}
	for i, b := range u.received() {
		if b == 1 {
			have = append(have, i)
		}
	}
	jsonAnswer(w, http.StatusOK, map[string]any{"size": u.size, "pieces": u.pieces, "have": have, "complete": false})
}

// used: the bytes the seeder keeps (its files and the uploads under way).
func (h *SeedHTTP) used() int64 {
	var total int64
	_ = filepath.Walk(h.S.Store.Dir, func(_ string, info os.FileInfo, err error) error {
		if err == nil && !info.IsDir() {
			total += info.Size()
		}
		return nil
	})
	return total
}

func (h *SeedHTTP) uploadPiece(w http.ResponseWriter, r *http.Request, bearer, sha, number string) {
	t := h.uploadTicket(w, bearer, sha)
	if t == nil {
		return
	}
	u := h.uploadOf(t)
	n, err := strconv.Atoi(number)
	if err != nil || n < 0 || n >= u.pieces {
		refuseHTTP(w, http.StatusConflict, fmt.Sprintf("The piece is from 0 to %d", u.pieces-1))
		return
	}
	want := int64(PieceSize)
	if n == u.pieces-1 {
		want = u.size - int64(n)*PieceSize
	}
	data, err := io.ReadAll(io.LimitReader(r.Body, want+1))
	if err != nil {
		refuseHTTP(w, http.StatusBadRequest, "The piece did not arrive whole")
		return
	}
	if int64(len(data)) > want {
		refuseHTTP(w, http.StatusRequestEntityTooLarge, fmt.Sprintf("Piece %d has %d bytes at most (size %d)", n, want, u.size))
		return
	}
	if int64(len(data)) != want {
		refuseHTTP(w, http.StatusConflict, fmt.Sprintf("Piece %d has %d bytes, not %d", n, want, len(data)))
		return
	}
	// A file it already has, or a piece received before: nothing to do.
	if h.S.Store.Has(sha) {
		jsonAnswer(w, http.StatusOK, map[string]any{"have": u.pieces, "pieces": u.pieces, "complete": true})
		return
	}
	h.mu.Lock()
	if h.busy == nil {
		h.busy = map[string]bool{}
	}
	if h.busy[u.part] {
		h.mu.Unlock()
		w.Header().Set("Retry-After", "5")
		refuseHTTP(w, http.StatusTooManyRequests, "This file is being checked: ask again in a moment")
		return
	}
	h.busy[u.part] = true
	h.mu.Unlock()
	defer func() {
		h.mu.Lock()
		delete(h.busy, u.part)
		h.mu.Unlock()
	}()
	have := u.received()
	if have[n] == 0 {
		if _, err := os.Stat(u.part); os.IsNotExist(err) && h.S.MaxBytes > 0 && h.used()+u.size > h.S.MaxBytes {
			log.Printf("WARNING: no room for an upload of %d MB in TISMA_SEED_GB (%d GB): give the seeder more room",
				u.size>>20, h.S.MaxBytes>>30)
			refuseHTTP(w, http.StatusInsufficientStorage, "The library has no room for this file now")
			return
		}
		if allowed, wait := h.count(&h.up, t.Account, int64(len(data)), h.UploadBytes); !allowed {
			w.Header().Set("Retry-After", strconv.Itoa(wait))
			refuseHTTP(w, http.StatusTooManyRequests, "The upload limit of this account for this hour was reached")
			return
		}
		if err := writeAt(u.part, int64(n)*PieceSize, data); err != nil {
			refuseHTTP(w, http.StatusInsufficientStorage, "The piece could not be kept")
			return
		}
		have[n] = 1
		if err := os.WriteFile(u.have, have, 0o644); err != nil {
			refuseHTTP(w, http.StatusInsufficientStorage, "The piece could not be kept")
			return
		}
	}
	count := 0
	for _, b := range have {
		count += int(b)
	}
	if count < u.pieces {
		jsonAnswer(w, http.StatusOK, map[string]any{"have": count, "pieces": u.pieces})
		return
	}
	h.finish(w, t, u)
}

func writeAt(path string, offset int64, data []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE, 0o644)
	if err != nil {
		return err
	}
	if _, err := f.WriteAt(data, offset); err != nil {
		f.Close()
		return err
	}
	return f.Close()
}

// finish checks a whole upload: its SHA-256, the rules of 3 octies and ClamAV; then it is kept and served.
func (h *SeedHTTP) finish(w http.ResponseWriter, t *Ticket, u upload) {
	drop := func(why string) {
		_ = os.Remove(u.part)
		_ = os.Remove(u.have)
		refuseHTTP(w, http.StatusUnprocessableEntity, why)
	}
	f, err := os.Open(u.part)
	if err != nil {
		drop("The file could not be read back")
		return
	}
	sum := sha256.New()
	size, err := io.Copy(sum, f)
	f.Close()
	if err != nil || size != u.size || hex.EncodeToString(sum.Sum(nil)) != t.SHA256 {
		drop("The file sent is not the one of the ticket (its SHA-256 differs): upload it again")
		return
	}
	if err := SafeFile(u.part); err != nil {
		drop("Not accepted in the library: " + err.Error())
		return
	}
	if h.S.ClamAV != "" {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
		found, err := ClamAV(ctx, h.S.ClamAV, u.part)
		cancel()
		if err != nil {
			// The antivirus did not answer: the pieces are kept and the check runs again at the next piece sent.
			log.Printf("ClamAV did not run on an upload (%v)", err)
			w.Header().Set("Retry-After", "60")
			refuseHTTP(w, http.StatusServiceUnavailable, "The file could not be checked for viruses now: send its last piece again in a minute")
			return
		}
		if found != "" {
			// What it found, without the seeder's folders (clamscan names the file by its path).
			drop("Not accepted in the library: the antivirus found " + strings.TrimPrefix(found, u.part+": "))
			return
		}
	}
	if err := os.Rename(u.part, h.S.Store.path(t.SHA256)); err != nil {
		refuseHTTP(w, http.StatusInsufficientStorage, "The file could not be kept")
		return
	}
	_ = os.Remove(u.have)
	h.S.Uploaded(t.SHA256)
	log.Printf("uploaded by its author: %s (%d MB)", t.SHA256, u.size>>20)
	jsonAnswer(w, http.StatusCreated, map[string]any{"complete": true})
}
