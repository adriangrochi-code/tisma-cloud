package main

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"io"
	"strings"
	"testing"
	"time"

	"github.com/libp2p/go-libp2p"
	"github.com/libp2p/go-libp2p/core/crypto"
	"github.com/libp2p/go-libp2p/core/host"
	"github.com/libp2p/go-libp2p/core/metrics"
	"github.com/libp2p/go-libp2p/core/network"
	"github.com/libp2p/go-libp2p/core/peer"
	"github.com/libp2p/go-libp2p/p2p/net/swarm"
	"github.com/libp2p/go-libp2p/p2p/protocol/circuitv2/client"
	ma "github.com/multiformats/go-multiaddr"
)

// sign makes a ticket as the master does (peers.ticket: canonical JSON, sorted keys).
func sign(t *testing.T, key ed25519.PrivateKey, account string, id peer.ID, expires time.Time, capMB int64) string {
	t.Helper()
	payload, _ := json.Marshal(map[string]any{"account": account, "cap_mb_per_hour": capMB, "expires": expires.Unix(),
		"peer_id": id.String()})
	enc := base64.RawURLEncoding
	return enc.EncodeToString(payload) + "." + enc.EncodeToString(ed25519.Sign(key, payload))
}

func newKey(t *testing.T) (ed25519.PublicKey, ed25519.PrivateKey) {
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return pub, priv
}

func TestTickets(t *testing.T) {
	pub, priv := newKey(t)
	_, other := newKey(t)
	a, b := peer.ID("12D3KooWA"), peer.ID("12D3KooWB")
	now := time.Now()
	if _, err := ParseTicket(sign(t, priv, "acc", a, now.Add(time.Hour), 100), pub, a, now); err != nil {
		t.Fatalf("a good ticket: %v", err)
	}
	cases := map[string]string{
		"signed by another key": sign(t, other, "acc", a, now.Add(time.Hour), 100),
		"of another peer":       sign(t, priv, "acc", b, now.Add(time.Hour), 100),
		"ended":                 sign(t, priv, "acc", a, now.Add(-time.Second), 100),
		"no traffic":            sign(t, priv, "acc", a, now.Add(time.Hour), 0),
		"not a ticket":          "hello",
	}
	for why, text := range cases {
		if _, err := ParseTicket(text, pub, a, now); err == nil {
			t.Errorf("%s: accepted", why)
		}
	}
}

func TestLibraryKeyNeedsTheProjectsCertificate(t *testing.T) {
	project, projectPriv := newKey(t)
	library, _ := newKey(t)
	cert := ed25519.Sign(projectPriv, append(append([]byte{}, CertPrefix...), library...))
	b64 := base64.StdEncoding.EncodeToString
	if _, err := LibraryKey(b64(library), b64(cert), hex.EncodeToString(project)); err != nil {
		t.Fatal(err)
	}
	other, _ := newKey(t)
	if _, err := LibraryKey(b64(library), b64(cert), hex.EncodeToString(other)); err == nil {
		t.Fatal("a certificate of another project key was accepted")
	}
}

type world struct {
	relay   host.Host
	gate    *Gate
	counter *metrics.BandwidthCounter
	key     ed25519.PrivateKey
}

func newWorld(t *testing.T) *world {
	pub, priv := newKey(t)
	gate := NewGate(pub, nil)
	counter := metrics.NewBandwidthCounter()
	r, err := newHost(mustKey(t), []string{"/ip4/127.0.0.1/tcp/0"}, nil, gate, counter)
	if err != nil {
		t.Fatal(err)
	}
	gate.Serve(r)
	t.Cleanup(func() { r.Close() })
	return &world{relay: r, gate: gate, counter: counter, key: priv}
}

func mustKey(t *testing.T) crypto.PrivKey {
	key, _, err := crypto.GenerateEd25519Key(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return key
}

func app(t *testing.T) host.Host {
	h, err := libp2p.New(libp2p.ListenAddrStrings("/ip4/127.0.0.1/tcp/0"), libp2p.EnableRelay())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { h.Close() })
	return h
}

func (w *world) info() peer.AddrInfo { return peer.AddrInfo{ID: w.relay.ID(), Addrs: w.relay.Addrs()} }

// present sends a ticket over the ticket protocol, as an app does, and returns the relay's answer.
func (w *world) present(t *testing.T, h host.Host, ticket string) map[string]any {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := h.Connect(ctx, w.info()); err != nil {
		t.Fatal(err)
	}
	s, err := h.NewStream(ctx, w.relay.ID(), TicketProtocol)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	if _, err := s.Write([]byte(ticket + "\n")); err != nil {
		t.Fatal(err)
	}
	line, err := io.ReadAll(s)
	if err != nil {
		t.Fatal(err)
	}
	var answer map[string]any
	if err := json.Unmarshal(bytes.TrimSpace(line), &answer); err != nil {
		t.Fatalf("answer %q: %v", line, err)
	}
	return answer
}

const echo = "/tisma/test-echo/1"

// through opens a stream from b to a through the relay and sends n bytes; it returns how many came back.
func (w *world) through(t *testing.T, a, b host.Host, n int) (int, error) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	circuit, _ := ma.NewMultiaddr("/p2p/" + w.relay.ID().String() + "/p2p-circuit/p2p/" + a.ID().String())
	var full []ma.Multiaddr
	for _, addr := range w.relay.Addrs() {
		full = append(full, addr.Encapsulate(circuit))
	}
	b.Peerstore().ClearAddrs(a.ID())
	// A dial refused before (no ticket yet) leaves a backoff; this one is after the ticket.
	if sw, ok := b.Network().(*swarm.Swarm); ok {
		sw.Backoff().Clear(a.ID())
	}
	if err := b.Connect(ctx, peer.AddrInfo{ID: a.ID(), Addrs: full}); err != nil {
		return 0, err
	}
	s, err := b.NewStream(network.WithAllowLimitedConn(ctx, "test"), a.ID(), echo)
	if err != nil {
		return 0, err
	}
	defer s.Close()
	go func() {
		_, _ = s.Write(bytes.Repeat([]byte("x"), n))
		_ = s.CloseWrite()
	}()
	got, err := io.Copy(io.Discard, s)
	return int(got), err
}

func listen(a host.Host) {
	a.SetStreamHandler(echo, func(s network.Stream) {
		defer s.Close()
		_, _ = io.Copy(s, s)
	})
}

func TestOnlyTicketedPeersReserveAndRelay(t *testing.T) {
	w := newWorld(t)
	a, b := app(t), app(t)
	listen(a)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	// Without a ticket: no reservation.
	if err := a.Connect(ctx, w.info()); err != nil {
		t.Fatal(err)
	}
	if _, err := client.Reserve(ctx, a, w.info()); err == nil {
		t.Fatal("a peer without a ticket reserved")
	}
	// A bad ticket is refused, with the reason.
	if answer := w.present(t, a, sign(t, w.key, "acc-a", b.ID(), time.Now().Add(time.Hour), 100)); answer["ok"] != false ||
		!strings.Contains(answer["error"].(string), "another peer") {
		t.Fatalf("someone else's ticket: %v", answer)
	}
	if answer := w.present(t, a, sign(t, w.key, "acc-a", a.ID(), time.Now().Add(time.Hour), 100)); answer["ok"] != true {
		t.Fatalf("a's ticket: %v", answer)
	}
	if _, err := client.Reserve(ctx, a, w.info()); err != nil {
		t.Fatalf("reserve with a ticket: %v", err)
	}
	// b has no ticket yet: no circuit.
	if _, err := w.through(t, a, b, 1000); err == nil {
		t.Fatal("a peer without a ticket was relayed")
	}
	w.present(t, b, sign(t, w.key, "acc-b", b.ID(), time.Now().Add(time.Hour), 100))
	got, err := w.through(t, a, b, 3<<20)
	if err != nil || got != 3<<20 {
		t.Fatalf("relayed %d of %d bytes: %v", got, 3<<20, err)
	}
}

func TestAnAccountOverItsCapIsCutUntilTheNextHour(t *testing.T) {
	w := newWorld(t)
	a, b := app(t), app(t)
	listen(a)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	// Two hours: the test moves the clock to the next hour.
	w.present(t, a, sign(t, w.key, "acc-a", a.ID(), time.Now().Add(2*time.Hour), 1))
	w.present(t, b, sign(t, w.key, "acc-b", b.ID(), time.Now().Add(2*time.Hour), 100))
	if _, err := client.Reserve(ctx, a, w.info()); err != nil {
		t.Fatal(err)
	}
	if got, err := w.through(t, a, b, 3<<20); err != nil || got != 3<<20 {
		t.Fatalf("relayed %d: %v", got, err)
	}
	// The meter counts a's 1 MB of this hour as gone: a is cut and refused. (The counter adds what it saw once a
	// second.)
	var cut []peer.ID
	for deadline := time.Now().Add(5 * time.Second); time.Now().Before(deadline) && len(cut) == 0; {
		time.Sleep(200 * time.Millisecond)
		cut = w.gate.Count(func(id peer.ID) int64 {
			s := w.counter.GetBandwidthForPeer(id)
			return s.TotalIn + s.TotalOut
		})
	}
	if len(cut) != 1 || cut[0] != a.ID() {
		t.Fatalf("cut %v, want only a", cut)
	}
	if w.gate.AllowReserve(a.ID(), nil) || w.gate.AllowConnect(b.ID(), nil, a.ID()) {
		t.Fatal("an account over its cap is still allowed")
	}
	if !w.gate.AllowReserve(b.ID(), nil) {
		t.Fatal("the other account was cut too")
	}
	// The next hour, it is allowed again.
	later := time.Now().Add(time.Hour)
	w.gate.now = func() time.Time { return later }
	w.gate.Count(func(peer.ID) int64 { return 0 })
	if !w.gate.AllowReserve(a.ID(), nil) {
		t.Fatal("not allowed again the next hour")
	}
}

func TestTheSignedBlocklistStopsAnAccountAndAPeer(t *testing.T) {
	w := newWorld(t)
	a, b := app(t), app(t)
	w.present(t, a, sign(t, w.key, "acc-a", a.ID(), time.Now().Add(time.Hour), 100))
	w.present(t, b, sign(t, w.key, "acc-b", b.ID(), time.Now().Add(time.Hour), 100))
	pubB, _ := b.Peerstore().PubKey(b.ID()).Raw()
	w.gate.Block([]string{"acc-a"}, []string{base64.StdEncoding.EncodeToString(pubB)})
	if w.gate.AllowReserve(a.ID(), nil) || w.gate.AllowReserve(b.ID(), nil) {
		t.Fatal("blocked account or peer still allowed")
	}
	if answer := w.present(t, a, sign(t, w.key, "acc-a", a.ID(), time.Now().Add(time.Hour), 100)); answer["ok"] != false {
		t.Fatalf("a blocked account got a ticket in: %v", answer)
	}
}

// A ticket made by the master's own code (tisma_cloud.peers.ticket, Python) with fixed keys: the relay reads it.
func TestATicketOfTheMastersCode(t *testing.T) {
	lib, _ := hex.DecodeString("03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8")
	from, err := peer.Decode("12D3KooWCd3eX8r5ihRvzK7P1yPq5aakaBJhG5GNj18YTztPhoCa")
	if err != nil {
		t.Fatal(err)
	}
	ticket := "eyJhY2NvdW50IjoidWFfdGVzdCIsImNhcF9tYl9wZXJfaG91ciI6MjAwLCJleHBpcmVzIjo0MTAyNDQ0ODAwLCJwZWVyX2lkIjoiMTJE" +
		"M0tvb1dDZDNlWDhyNWloUnZ6SzdQMXlQcTVhYWthQkpoRzVHTmoxOFlUenRQaG9DYSJ9.EfquZVBT2FiE-EG8c7ihRq-Yo_dku8vd2q8rK6NC6" +
		"n1DpU7C81m055dYeAMpWaAvBdnvvp151z8epJjVgMI0Ag"
	got, err := ParseTicket(ticket, lib, from, time.Unix(4102444800-60, 0))
	if err != nil {
		t.Fatal(err)
	}
	if got.Account != "ua_test" || got.CapMBPerHour != 200 {
		t.Fatalf("read %+v", got)
	}
}
