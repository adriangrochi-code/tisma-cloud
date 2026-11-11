// The protocol between peers of the library (p2p_library.md 8 septies): JSON lines have, pieces and get over an
// encrypted stream; get answers with the raw bytes of a piece of 1 MiB.
package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sync"
	"time"

	"github.com/libp2p/go-libp2p/core/host"
	"github.com/libp2p/go-libp2p/core/network"
	"github.com/libp2p/go-libp2p/core/peer"
	"github.com/libp2p/go-libp2p/core/protocol"
)

const (
	LibraryProtocol = protocol.ID("/tisma/library/1.0.0")
	PieceSize       = 1 << 20
	maxRequestLine  = 64 << 10
	maxHave         = 256
)

var sha256Hex = regexp.MustCompile(`^[0-9a-f]{64}$`)

// Store keeps whole files by their SHA-256 in a folder (<sha256>, and <sha256>.part while being made).
type Store struct {
	Dir string
	mu  sync.Mutex
}

func (s *Store) path(sha string) string { return filepath.Join(s.Dir, sha) }

func (s *Store) Has(sha string) bool {
	_, err := os.Stat(s.path(sha))
	return err == nil
}

func (s *Store) Remove(sha string) { _ = os.Remove(s.path(sha)) }

// Hashes are the SHA-256 of each piece of a whole file.
func (s *Store) Hashes(sha string) (int64, [][]byte, error) {
	f, err := os.Open(s.path(sha))
	if err != nil {
		return 0, nil, err
	}
	defer f.Close()
	var hashes [][]byte
	var size int64
	buf := make([]byte, PieceSize)
	for {
		n, err := io.ReadFull(f, buf)
		if n > 0 {
			h := sha256.Sum256(buf[:n])
			hashes = append(hashes, h[:])
			size += int64(n)
		}
		if err == io.EOF || err == io.ErrUnexpectedEOF {
			break
		}
		if err != nil {
			return 0, nil, err
		}
	}
	return size, hashes, nil
}

// PieceRoot: the SHA-256 of the raw piece hashes one after the other (piece_roots of the index).
func PieceRoot(hashes [][]byte) string {
	h := sha256.New()
	for _, p := range hashes {
		h.Write(p)
	}
	return hex.EncodeToString(h.Sum(nil))
}

// Policy says which files may be served now (in the signed index, not blocked) and whether the upload cap is reached.
type Policy interface {
	Servable(sha string) (ok bool, blocked bool)
	Busy() bool
	Sent(n int)
}

type request struct {
	Op    string   `json:"op"`
	Files []string `json:"files"`
	File  string   `json:"file"`
	Piece *int     `json:"piece"`
}

func answer(w io.Writer, v any) error {
	out, err := json.Marshal(v)
	if err != nil {
		return err
	}
	_, err = w.Write(append(out, '\n'))
	return err
}

func refuse(w io.Writer, code string) error {
	return answer(w, map[string]any{"ok": false, "error": code})
}

// ServeLibrary answers have, pieces and get for the files of the store the policy allows.
func ServeLibrary(h host.Host, store *Store, policy Policy) {
	h.SetStreamHandler(LibraryProtocol, func(s network.Stream) {
		defer s.Close()
		r := bufio.NewReaderSize(s, 4096)
		for {
			_ = s.SetReadDeadline(time.Now().Add(2 * time.Minute))
			line, err := readLine(r)
			if err != nil {
				if !errors.Is(err, io.EOF) {
					_ = refuse(s, "bad_request")
				}
				return
			}
			var req request
			if json.Unmarshal(line, &req) != nil {
				_ = refuse(s, "bad_request")
				return
			}
			_ = s.SetWriteDeadline(time.Now().Add(2 * time.Minute))
			if !serveOne(s, store, policy, req) {
				return
			}
		}
	})
}

func readLine(r *bufio.Reader) ([]byte, error) {
	var line []byte
	for {
		part, more, err := r.ReadLine()
		if err != nil {
			return nil, err
		}
		line = append(line, part...)
		if len(line) > maxRequestLine {
			return nil, errors.New("too long")
		}
		if !more {
			return line, nil
		}
	}
}

// serveOne answers one request; false closes the stream (a bad request).
func serveOne(w io.Writer, store *Store, policy Policy, req request) bool {
	switch req.Op {
	case "have":
		if len(req.Files) == 0 || len(req.Files) > maxHave {
			_ = refuse(w, "bad_request")
			return false
		}
		have := map[string]string{}
		for _, sha := range req.Files {
			if !sha256Hex.MatchString(sha) {
				_ = refuse(w, "bad_request")
				return false
			}
			if ok, _ := policy.Servable(sha); ok && store.Has(sha) {
				have[sha] = "all"
			}
		}
		return answer(w, map[string]any{"ok": true, "have": have}) == nil
	case "pieces", "get":
		if !sha256Hex.MatchString(req.File) || (req.Op == "get" && (req.Piece == nil || *req.Piece < 0)) {
			_ = refuse(w, "bad_request")
			return false
		}
		if ok, blocked := policy.Servable(req.File); !ok {
			code := "not_found"
			if blocked {
				code = "blocked"
			}
			return refuse(w, code) == nil
		}
		if !store.Has(req.File) {
			return refuse(w, "not_found") == nil
		}
		if req.Op == "pieces" {
			size, hashes, err := store.Hashes(req.File)
			if err != nil {
				return refuse(w, "not_found") == nil
			}
			list := make([]string, len(hashes))
			for i, h := range hashes {
				list[i] = hex.EncodeToString(h)
			}
			return answer(w, map[string]any{"ok": true, "size": size, "hashes": list}) == nil
		}
		if policy.Busy() {
			return refuse(w, "busy") == nil
		}
		piece, err := readPiece(store.path(req.File), *req.Piece)
		if err != nil {
			return refuse(w, "not_found") == nil
		}
		if answer(w, map[string]any{"ok": true, "length": len(piece)}) != nil {
			return false
		}
		n, err := w.Write(piece)
		policy.Sent(n)
		return err == nil
	default:
		_ = refuse(w, "bad_request")
		return false
	}
}

func readPiece(path string, n int) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	buf := make([]byte, PieceSize)
	got, err := f.ReadAt(buf, int64(n)*PieceSize)
	if got == 0 {
		return nil, errors.New("no such piece")
	}
	if err != nil && err != io.EOF {
		return nil, err
	}
	return buf[:got], nil
}

// Errors of a peer that the downloader drops (P2P2, P2P10).
var (
	ErrBadList  = errors.New("the piece list does not give the index's piece_roots")
	ErrBadPiece = errors.New("a piece does not match its hash")
)

type remoteError string

func (e remoteError) Error() string { return "the peer answered " + string(e) }

// Fetch downloads a whole file from one peer into the store, checking the piece list against pieceRoot, every piece
// against the list and the whole file against its SHA-256.
func Fetch(ctx context.Context, h host.Host, from peer.ID, store *Store, sha, pieceRoot string) error {
	s, err := h.NewStream(network.WithAllowLimitedConn(ctx, "library"), from, LibraryProtocol)
	if err != nil {
		return err
	}
	defer s.Close()
	if dl, ok := ctx.Deadline(); ok {
		_ = s.SetDeadline(dl)
	}
	r := bufio.NewReaderSize(s, 64<<10)
	call := func(req map[string]any, out any) error {
		if err := answer(s, req); err != nil {
			return err
		}
		line, err := readLine(r)
		if err != nil {
			return err
		}
		var head struct {
			OK    bool   `json:"ok"`
			Error string `json:"error"`
		}
		if err := json.Unmarshal(line, &head); err != nil {
			return err
		}
		if !head.OK {
			return remoteError(head.Error)
		}
		return json.Unmarshal(line, out)
	}
	var list struct {
		Size   int64    `json:"size"`
		Hashes []string `json:"hashes"`
	}
	if err := call(map[string]any{"op": "pieces", "file": sha}, &list); err != nil {
		return err
	}
	var raw [][]byte
	for _, x := range list.Hashes {
		b, err := hex.DecodeString(x)
		if err != nil || len(b) != sha256.Size {
			return ErrBadList
		}
		raw = append(raw, b)
	}
	if PieceRoot(raw) != pieceRoot || int64(len(raw)) != (list.Size+PieceSize-1)/PieceSize {
		return ErrBadList
	}
	store.mu.Lock()
	part := store.path(sha) + ".part"
	store.mu.Unlock()
	f, err := os.Create(part)
	if err != nil {
		return err
	}
	whole := sha256.New()
	ok := false
	defer func() {
		f.Close()
		if !ok {
			os.Remove(part)
		}
	}()
	for i, want := range raw {
		var head struct {
			Length int `json:"length"`
		}
		if err := call(map[string]any{"op": "get", "file": sha, "piece": i}, &head); err != nil {
			return err
		}
		if head.Length <= 0 || head.Length > PieceSize {
			return ErrBadPiece
		}
		piece := make([]byte, head.Length)
		if _, err := io.ReadFull(r, piece); err != nil {
			return err
		}
		if got := sha256.Sum256(piece); !bytes.Equal(got[:], want) {
			return ErrBadPiece
		}
		whole.Write(piece)
		if _, err := f.Write(piece); err != nil {
			return err
		}
	}
	if hex.EncodeToString(whole.Sum(nil)) != sha {
		return fmt.Errorf("the file is not %s", sha)
	}
	if err := f.Sync(); err != nil {
		return err
	}
	f.Close()
	if err := os.Rename(part, store.path(sha)); err != nil {
		return err
	}
	ok = true
	return nil
}
