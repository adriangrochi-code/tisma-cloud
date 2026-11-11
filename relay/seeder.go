// The seeder of Tisma Cloud (p2p_library.md rule 3 and 3 octies): it keeps the models of the signed index so that they
// are always there, serves them with /tisma/library/1.0.0 like the apps, checks what it downloads (3 octies and
// ClamAV) and tells the master, and announces itself directly (8 quater: seeders share direct, without a relay).
package main

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"

	"github.com/libp2p/go-libp2p/core/crypto"
	"github.com/libp2p/go-libp2p/core/host"
	"github.com/libp2p/go-libp2p/core/peer"
	ma "github.com/multiformats/go-multiaddr"
)

// Master talks to the master's library routes with the seeder's account (an internal account of Tisma).
type Master struct {
	URL   string
	Token string
	Key   ed25519.PublicKey // the library key, checked with its certificate
	HTTP  *http.Client
}

func (m *Master) do(method, path string, body any, out any) error {
	var r io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		r = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, m.URL+path, r)
	if err != nil {
		return err
	}
	if m.Token != "" {
		req.Header.Set("Authorization", "Bearer "+m.Token)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	res, err := m.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	data, err := io.ReadAll(io.LimitReader(res.Body, 64<<20))
	if err != nil {
		return err
	}
	if res.StatusCode/100 != 2 {
		return fmt.Errorf("%s %s: HTTP %d %s", method, path, res.StatusCode, bytes.TrimSpace(data))
	}
	if out != nil {
		return json.Unmarshal(data, out)
	}
	return nil
}

// signedList downloads a list of the library and its signature, and checks it with the library key.
func (m *Master) signedList(name string, out any) error {
	get := func(p string) ([]byte, error) {
		res, err := m.HTTP.Get(m.URL + p)
		if err != nil {
			return nil, err
		}
		defer res.Body.Close()
		if res.StatusCode != http.StatusOK {
			return nil, fmt.Errorf("%s: HTTP %d", p, res.StatusCode)
		}
		return io.ReadAll(io.LimitReader(res.Body, 256<<20))
	}
	body, err := get("/v1/library/" + name + ".json")
	if err != nil {
		return err
	}
	sig, err := get("/v1/library/" + name + ".json.sig")
	if err != nil {
		return err
	}
	if !ed25519.Verify(m.Key, body, sig) {
		return fmt.Errorf("the %s is not signed by the library key", name)
	}
	return json.Unmarshal(body, out)
}

type indexFile struct {
	Path   string `json:"path"`
	Size   int64  `json:"size"`
	SHA256 string `json:"sha256"`
}

type indexModel struct {
	ID        string      `json:"id"`
	Revision  int         `json:"revision"`
	Published string      `json:"published"`
	Size      int64       `json:"size"`
	Files     []indexFile `json:"files"`
	Author    struct {
		Account string `json:"account"`
	} `json:"author"`
	PeerKey string `json:"peer_key"`
	Checked bool   `json:"checked"`
	Swarm   struct {
		PieceRoots map[string]string `json:"piece_roots"`
	} `json:"swarm"`
}

type blocklist struct {
	Files []struct {
		SHA256 string `json:"sha256"`
	} `json:"files"`
	Models []struct {
		ID string `json:"id"`
	} `json:"models"`
	Accounts []string `json:"accounts"`
	Peers    []string `json:"peers"`
}

// Seeder keeps the library's files and decides what may be served.
type Seeder struct {
	Master   *Master
	Host     host.Host
	PeerKey  string // the seeder's peer key (its libp2p identity), base64
	Sign     func([]byte) ([]byte, error)
	Store    *Store
	ClamAV   string   // clamdscan or clamscan
	Announce []string // its public addresses (direct)
	MaxBytes int64    // the space for the library
	CapBytes int64    // upload per hour

	mu       sync.Mutex
	servable map[string]bool // files of models in the index, not blocked
	blocked  map[string]bool // files blocked (or of blocked models or accounts)
	reported map[string]bool // revisions told to the master ("id/rev")
	window   time.Time
	sentHour int64
	now      func() time.Time
}

func (s *Seeder) clock() time.Time {
	if s.now != nil {
		return s.now()
	}
	return time.Now()
}

// Servable, Busy and Sent: the Policy of ServeLibrary.
func (s *Seeder) Servable(sha string) (bool, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.blocked[sha] {
		return false, true
	}
	return s.servable[sha], false
}

func (s *Seeder) Busy() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.roll()
	return s.CapBytes > 0 && s.sentHour >= s.CapBytes
}

func (s *Seeder) Sent(n int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.roll()
	s.sentHour += int64(n)
}

func (s *Seeder) roll() {
	if hour := s.clock().Truncate(time.Hour); s.window.Before(hour) {
		s.window, s.sentHour = hour, 0
	}
}

// Bind ties the seeder's peer key to its account (POST /v1/library/peer-key).
func (s *Seeder) Bind() error {
	return s.Master.do("POST", "/v1/library/peer-key", map[string]string{"public_key": s.PeerKey}, nil)
}

// models: the index's models that are not blocked, newest first, and the policy of what may be served.
func (s *Seeder) refresh() ([]indexModel, error) {
	var index struct {
		Models []indexModel `json:"models"`
	}
	if err := s.Master.signedList("index", &index); err != nil {
		return nil, err
	}
	var bl blocklist
	if err := s.Master.signedList("blocklist", &bl); err != nil {
		return nil, err
	}
	blockedModels, blockedAccounts, blockedPeers := map[string]bool{}, map[string]bool{}, map[string]bool{}
	for _, m := range bl.Models {
		blockedModels[m.ID] = true
	}
	for _, a := range bl.Accounts {
		blockedAccounts[a] = true
	}
	for _, p := range bl.Peers {
		blockedPeers[p] = true
	}
	blocked, servable := map[string]bool{}, map[string]bool{}
	for _, f := range bl.Files {
		blocked[f.SHA256] = true
	}
	var keep []indexModel
	for _, m := range index.Models {
		bad := blockedModels[m.ID] || blockedAccounts[m.Author.Account] || blockedPeers[m.PeerKey]
		for _, f := range m.Files {
			if bad {
				blocked[f.SHA256] = true
			}
		}
		if !bad {
			keep = append(keep, m)
		}
	}
	for _, m := range keep {
		for _, f := range m.Files {
			if !blocked[f.SHA256] {
				servable[f.SHA256] = true
			}
		}
	}
	sort.SliceStable(keep, func(i, j int) bool { return keep[i].Published > keep[j].Published })
	s.mu.Lock()
	s.servable, s.blocked = servable, blocked
	if s.reported == nil {
		s.reported = map[string]bool{}
	}
	s.mu.Unlock()
	return keep, nil
}

// Cycle: what the seeder does every few minutes. It returns the models it has whole.
func (s *Seeder) Cycle(ctx context.Context) ([]string, error) {
	models, err := s.refresh()
	if err != nil {
		return nil, err
	}
	// Files no longer in the index, or blocked: deleted (P2P3).
	entries, _ := os.ReadDir(s.Store.Dir)
	for _, e := range entries {
		if ok, _ := s.Servable(e.Name()); !ok && sha256Hex.MatchString(e.Name()) {
			s.Store.Remove(e.Name())
		}
	}
	var whole []string
	var used int64
	for _, m := range models {
		if s.MaxBytes > 0 && used+m.Size > s.MaxBytes {
			continue
		}
		used += m.Size
		if err := s.fetchModel(ctx, m); err != nil {
			log.Printf("model %s: %v", m.ID, err)
			continue
		}
		whole = append(whole, m.ID)
		s.check(ctx, m)
	}
	if err := s.announce(whole); err != nil {
		log.Printf("announce: %v", err)
	}
	return whole, nil
}

// fetchModel downloads the files of a model it does not have yet from the peers the master gives.
func (s *Seeder) fetchModel(ctx context.Context, m indexModel) error {
	var missing []indexFile
	for _, f := range m.Files {
		if ok, _ := s.Servable(f.SHA256); ok && !s.Store.Has(f.SHA256) {
			missing = append(missing, f)
		}
	}
	if len(missing) == 0 {
		return nil
	}
	var found struct {
		Peers []struct {
			PeerID string   `json:"peer_id"`
			Addrs  []string `json:"addrs"`
		} `json:"peers"`
	}
	q := url.Values{"model": {m.ID}, "peer_key": {s.PeerKey}}
	if err := s.Master.do("GET", "/v1/library/peers?"+q.Encode(), nil, &found); err != nil {
		return err
	}
	for _, f := range missing {
		root := m.Swarm.PieceRoots[f.SHA256]
		var last error = errors.New("no peer has it")
		for _, p := range found.Peers {
			id, err := peer.Decode(p.PeerID)
			if err != nil || id == s.Host.ID() {
				continue
			}
			var addrs []ma.Multiaddr
			for _, a := range p.Addrs {
				if x, err := ma.NewMultiaddr(a); err == nil {
					addrs = append(addrs, x)
				}
			}
			cctx, cancel := context.WithTimeout(ctx, 10*time.Minute)
			err = s.Host.Connect(cctx, peer.AddrInfo{ID: id, Addrs: addrs})
			if err == nil {
				err = Fetch(cctx, s.Host, id, s.Store, f.SHA256, root)
			}
			cancel()
			if err == nil {
				last = nil
				break
			}
			// A peer that sends a wrong list or a wrong piece is left (P2P2, P2P10): the next one is asked.
			last = err
		}
		if last != nil {
			return fmt.Errorf("%s: %w", f.Path, last)
		}
	}
	return nil
}

// check runs the checks of 3 octies and ClamAV on a whole revision once, and tells the master.
func (s *Seeder) check(ctx context.Context, m indexModel) {
	key := fmt.Sprintf("%s/%d", m.ID, m.Revision)
	s.mu.Lock()
	done := s.reported[key] || m.Checked
	s.mu.Unlock()
	if done || s.ClamAV == "" {
		return
	}
	clean, detail := true, ""
	for _, f := range m.Files {
		file := filepath.Join(s.Store.Dir, f.SHA256)
		if err := SafeFile(file); err != nil {
			clean, detail = false, fmt.Sprintf("%s: %v", f.Path, err)
			break
		}
		found, err := ClamAV(ctx, s.ClamAV, file)
		if err != nil {
			log.Printf("ClamAV did not run (%v): %s is asked again later", err, m.ID)
			return
		}
		if found != "" {
			clean, detail = false, fmt.Sprintf("%s: %s", f.Path, found)
			break
		}
	}
	err := s.Master.do("POST", "/v1/library/models/"+url.PathEscape(m.ID)+"/check",
		map[string]any{"revision": m.Revision, "clean": clean, "detail": detail}, nil)
	if err != nil {
		log.Printf("check of %s: %v", key, err)
		return
	}
	if !clean {
		// Not served while a person reviews it.
		for _, f := range m.Files {
			s.mu.Lock()
			s.blocked[f.SHA256] = true
			s.mu.Unlock()
			s.Store.Remove(f.SHA256)
		}
	}
	s.mu.Lock()
	s.reported[key] = true
	s.mu.Unlock()
}

// announce tells the master which models it shares, directly (8 quater), signed with its peer key.
func (s *Seeder) announce(models []string) error {
	if len(s.Announce) == 0 {
		return nil
	}
	if models == nil {
		models = []string{}
	}
	when := s.clock().Unix()
	body := map[string]any{"addrs": s.Announce, "direct": true, "models": models, "peer_key": s.PeerKey, "time": when}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(body); err != nil { // sorted keys, no spaces: as peers.announcement_bytes
		return err
	}
	sig, err := s.Sign(bytes.TrimRight(buf.Bytes(), "\n"))
	if err != nil {
		return err
	}
	body["signature"] = base64.StdEncoding.EncodeToString(sig)
	return s.Master.do("POST", "/v1/library/peers/announce", body, nil)
}

// peerKeyOf is the base64 of the raw Ed25519 public key of the host's identity: the peer key of the library.
func peerKeyOf(key crypto.PrivKey) (string, error) {
	raw, err := key.GetPublic().Raw()
	if err != nil {
		return "", err
	}
	return base64.StdEncoding.EncodeToString(raw), nil
}
