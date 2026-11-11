// tisma-relay: the relay of Tisma's library (p2p_library.md 8 ter, 8 quater), a program apart from the master.
//
//	TISMA_MASTER_URL           the master (its library key and its signed blocklist), e.g. https://tismaservidores.net
//	TISMA_LIBRARY_PROJECT_KEY  the project's public key (64 hex digits): the library key is accepted with its certificate
//	TISMA_RELAY_LISTEN         addresses, comma separated (default /ip4/0.0.0.0/tcp/4001,/ip4/0.0.0.0/udp/4001/quic-v1)
//	TISMA_RELAY_KEY            the relay's own key (made the first time; its PeerId goes in TISMA_LIBRARY_RELAYS)
//	TISMA_RELAY_ANNOUNCE       public addresses to announce, comma separated (behind NAT or a forwarded port)
//
// tisma-relay seed: the seeder of the library (p2p_library.md rule 3) instead of the relay, with also
//
//	TISMA_SEED_TOKEN           a session of the seeder's account (an internal account of Tisma)
//	TISMA_SEED_DIR             where the files are kept (default ./library)
//	TISMA_SEED_GB              the space for them (default 50)
//	TISMA_SEED_MB_PER_HOUR     upload per hour (default 0: no cap)
//	TISMA_SEED_ANNOUNCE        its public addresses, announced to the master (it shares directly)
//	TISMA_SEED_KEY             its own key (default seed.key)
//	TISMA_CLAMAV               clamdscan or clamscan (default clamdscan; ClamAV is called, never linked: it is GPL)
package main

import (
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/libp2p/go-libp2p"
	"github.com/libp2p/go-libp2p/core/crypto"
	"github.com/libp2p/go-libp2p/core/host"
	"github.com/libp2p/go-libp2p/core/metrics"
	"github.com/libp2p/go-libp2p/p2p/protocol/circuitv2/relay"
	ma "github.com/multiformats/go-multiaddr"
)

func env(name, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(name)); v != "" {
		return v
	}
	return fallback
}

func split(text string) []string {
	var out []string
	for _, p := range strings.Split(text, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

// identity is the relay's key, kept in a file (made the first time) so that its PeerId does not change.
func identity(path string) (crypto.PrivKey, error) {
	if raw, err := os.ReadFile(path); err == nil {
		return crypto.UnmarshalPrivateKey(raw)
	}
	key, _, err := crypto.GenerateEd25519Key(nil)
	if err != nil {
		return nil, err
	}
	raw, err := crypto.MarshalPrivateKey(key)
	if err != nil {
		return nil, err
	}
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		return nil, err
	}
	return key, nil
}

var httpClient = &http.Client{Timeout: 30 * time.Second}

func get(url string) ([]byte, error) {
	r, err := httpClient.Get(url)
	if err != nil {
		return nil, err
	}
	defer r.Body.Close()
	if r.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("%s: HTTP %d", url, r.StatusCode)
	}
	return io.ReadAll(io.LimitReader(r.Body, 8<<20))
}

// fetchKey: the master's library key, checked with its certificate.
func fetchKey(master, project string) (ed25519.PublicKey, error) {
	body, err := get(master + "/v1/library/key")
	if err != nil {
		return nil, err
	}
	var k struct {
		PublicKey   string  `json:"public_key"`
		Certificate *string `json:"certificate"`
	}
	if err := json.Unmarshal(body, &k); err != nil {
		return nil, err
	}
	if k.Certificate == nil {
		return nil, errors.New("the master's library key has no certificate yet")
	}
	return LibraryKey(k.PublicKey, *k.Certificate, project)
}

// refreshBlocklist: the master's signed blocklist, checked with the library key, into the gate.
func refreshBlocklist(master string, key ed25519.PublicKey, gate *Gate) error {
	body, err := get(master + "/v1/library/blocklist.json")
	if err != nil {
		return err
	}
	signature, err := get(master + "/v1/library/blocklist.json.sig")
	if err != nil {
		return err
	}
	if !ed25519.Verify(key, body, signature) {
		return errors.New("the blocklist is not signed by the library key")
	}
	var list struct {
		Accounts    []string `json:"accounts"`
		Peers       []string `json:"peers"`
		RevokedKeys []string `json:"revoked_keys"`
	}
	if err := json.Unmarshal(body, &list); err != nil {
		return err
	}
	for _, revoked := range list.RevokedKeys {
		if revoked == base64.StdEncoding.EncodeToString(key) {
			return errors.New("the library key was revoked: certify a new one on the master")
		}
	}
	gate.Block(list.Accounts, list.Peers)
	return nil
}

func newHost(key crypto.PrivKey, listen, announce []string, gate *Gate, counter *metrics.BandwidthCounter) (host.Host, error) {
	opts := []libp2p.Option{libp2p.Identity(key), libp2p.ListenAddrStrings(listen...),
		libp2p.BandwidthReporter(counter), libp2p.EnableRelayService(
			relay.WithACL(gate),
			// No limit per circuit: the cap is per account and per hour (Gate.Count).
			relay.WithInfiniteLimits(),
			relay.WithResources(relay.Resources{ReservationTTL: time.Hour, MaxReservations: 4096, MaxCircuits: 64,
				BufferSize: 64 << 10, MaxReservationsPerIP: 64, MaxReservationsPerASN: 1024})),
		libp2p.ForceReachabilityPublic()}
	if len(announce) > 0 {
		var addrs []ma.Multiaddr
		for _, a := range announce {
			m, err := ma.NewMultiaddr(a)
			if err != nil {
				return nil, fmt.Errorf("TISMA_RELAY_ANNOUNCE: %w", err)
			}
			addrs = append(addrs, m)
		}
		opts = append(opts, libp2p.AddrsFactory(func([]ma.Multiaddr) []ma.Multiaddr { return addrs }))
	}
	return libp2p.New(opts...)
}

func main() {
	if len(os.Args) > 1 && os.Args[1] == "seed" {
		seedMain()
		return
	}
	master := strings.TrimRight(env("TISMA_MASTER_URL", ""), "/")
	project := env("TISMA_LIBRARY_PROJECT_KEY", "")
	if master == "" || project == "" {
		log.Fatal("TISMA_MASTER_URL and TISMA_LIBRARY_PROJECT_KEY are needed")
	}
	libraryKey, err := fetchKey(master, project)
	if err != nil {
		log.Fatalf("the library key: %v", err)
	}
	key, err := identity(env("TISMA_RELAY_KEY", "relay.key"))
	if err != nil {
		log.Fatalf("the relay's key: %v", err)
	}
	gate := NewGate(libraryKey, nil)
	counter := metrics.NewBandwidthCounter()
	h, err := newHost(key, split(env("TISMA_RELAY_LISTEN", "/ip4/0.0.0.0/tcp/4001,/ip4/0.0.0.0/udp/4001/quic-v1")),
		split(env("TISMA_RELAY_ANNOUNCE", "")), gate, counter)
	if err != nil {
		log.Fatalf("starting: %v", err)
	}
	defer h.Close()
	gate.Serve(h)
	sum := sha256.Sum256(libraryKey)
	log.Printf("tisma-relay %s, library key %x…", h.ID(), sum[:4])
	for _, a := range h.Addrs() {
		log.Printf("for TISMA_LIBRARY_RELAYS: %s/p2p/%s", a, h.ID())
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go gate.Meter(ctx, h, counter, 2*time.Second)
	go func() {
		for {
			if err := refreshBlocklist(master, libraryKey, gate); err != nil {
				log.Printf("blocklist: %v", err)
			}
			select {
			case <-ctx.Done():
				return
			case <-time.After(10 * time.Minute):
			}
		}
	}()
	<-ctx.Done()
}

func seedMain() {
	master := strings.TrimRight(env("TISMA_MASTER_URL", ""), "/")
	project, token := env("TISMA_LIBRARY_PROJECT_KEY", ""), env("TISMA_SEED_TOKEN", "")
	if master == "" || project == "" || token == "" {
		log.Fatal("TISMA_MASTER_URL, TISMA_LIBRARY_PROJECT_KEY and TISMA_SEED_TOKEN are needed")
	}
	libraryKey, err := fetchKey(master, project)
	if err != nil {
		log.Fatalf("the library key: %v", err)
	}
	key, err := identity(env("TISMA_SEED_KEY", "seed.key"))
	if err != nil {
		log.Fatalf("the seeder's key: %v", err)
	}
	h, err := libp2p.New(libp2p.Identity(key),
		libp2p.ListenAddrStrings(split(env("TISMA_RELAY_LISTEN", "/ip4/0.0.0.0/tcp/4002,/ip4/0.0.0.0/udp/4002/quic-v1"))...))
	if err != nil {
		log.Fatalf("starting: %v", err)
	}
	defer h.Close()
	dir := env("TISMA_SEED_DIR", "library")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		log.Fatal(err)
	}
	peerKey, err := peerKeyOf(key)
	if err != nil {
		log.Fatal(err)
	}
	gb, mb := envInt("TISMA_SEED_GB", 50), envInt("TISMA_SEED_MB_PER_HOUR", 0)
	s := &Seeder{Master: &Master{URL: master, Token: token, Key: libraryKey, HTTP: httpClient}, Host: h, PeerKey: peerKey,
		Sign: key.Sign, Store: &Store{Dir: dir}, ClamAV: env("TISMA_CLAMAV", "clamdscan"),
		Announce: split(env("TISMA_SEED_ANNOUNCE", "")), MaxBytes: gb << 30, CapBytes: mb << 20}
	ServeLibrary(h, s.Store, s)
	if err := s.Bind(); err != nil {
		log.Fatalf("binding the seeder's key to its account: %v", err)
	}
	log.Printf("tisma-relay seed %s in %s", h.ID(), dir)
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	for {
		if whole, err := s.Cycle(ctx); err != nil {
			log.Printf("seeding: %v", err)
		} else {
			log.Printf("seeding %d models", len(whole))
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(10 * time.Minute):
		}
	}
}

func envInt(name string, fallback int64) int64 {
	var v int64
	if _, err := fmt.Sscan(env(name, fmt.Sprint(fallback)), &v); err != nil || v < 0 {
		log.Fatalf("%s is a number", name)
	}
	return v
}
