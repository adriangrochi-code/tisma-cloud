// The relay of Tisma (p2p_library.md 8 ter and 8 quater): it passes the pieces between two apps with circuit relay v2,
// without reading them, only for peers with a ticket of the master, and counts the bytes of each account per hour.
package main

import (
	"bufio"
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"log"
	"strings"
	"sync"
	"time"

	"github.com/libp2p/go-libp2p/core/crypto"
	"github.com/libp2p/go-libp2p/core/host"
	"github.com/libp2p/go-libp2p/core/metrics"
	"github.com/libp2p/go-libp2p/core/network"
	"github.com/libp2p/go-libp2p/core/peer"
	"github.com/libp2p/go-libp2p/core/protocol"
	ma "github.com/multiformats/go-multiaddr"
)

// TicketProtocol: the app writes its ticket and "\n"; the relay answers one line of JSON.
const TicketProtocol = protocol.ID("/tisma/relay-ticket/1.0.0")

const maxTicketBytes = 4096

type account struct {
	capBytes  int64
	window    time.Time // the hour being counted
	used      int64
	overUntil time.Time // over its cap: refused until then
}

type ticketed struct {
	account string
	expires time.Time
	seen    int64 // the peer's bytes already counted (from the bandwidth counter)
}

// Gate decides who may reserve and relay, and counts the bytes.
type Gate struct {
	mu       sync.Mutex
	key      ed25519.PublicKey
	now      func() time.Time
	peers    map[peer.ID]*ticketed
	accounts map[string]*account
	blocked  map[string]bool  // accounts in the signed blocklist
	banned   map[peer.ID]bool // peer keys in the signed blocklist
}

func NewGate(key ed25519.PublicKey, now func() time.Time) *Gate {
	if now == nil {
		now = time.Now
	}
	return &Gate{key: key, now: now, peers: map[peer.ID]*ticketed{}, accounts: map[string]*account{},
		blocked: map[string]bool{}, banned: map[peer.ID]bool{}}
}

// Present takes a ticket from a peer: an error says why it is refused.
func (g *Gate) Present(from peer.ID, text string) (*Ticket, error) {
	t, err := ParseTicket(text, g.key, from, g.now())
	if err != nil {
		return nil, err
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.blocked[t.Account] || g.banned[from] {
		return nil, errBlocked
	}
	p := g.peers[from]
	if p == nil {
		p = &ticketed{}
		g.peers[from] = p
	}
	p.account, p.expires = t.Account, time.Unix(t.Expires, 0)
	a := g.accounts[t.Account]
	if a == nil {
		a = &account{window: g.now().Truncate(time.Hour)}
		g.accounts[t.Account] = a
	}
	a.capBytes = t.CapMBPerHour << 20
	return t, nil
}

type gateError string

func (e gateError) Error() string { return string(e) }

const errBlocked = gateError("blocked in the library")

// allowed: a peer with a ticket in force, not blocked, whose account is under its cap this hour.
func (g *Gate) allowed(p peer.ID) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	t := g.peers[p]
	now := g.now()
	if t == nil || !now.Before(t.expires) || g.banned[p] || g.blocked[t.account] {
		return false
	}
	a := g.accounts[t.account]
	return a != nil && !now.Before(a.overUntil)
}

// AllowReserve and AllowConnect: the ACL of circuit relay v2.
func (g *Gate) AllowReserve(p peer.ID, _ ma.Multiaddr) bool { return g.allowed(p) }

func (g *Gate) AllowConnect(src peer.ID, _ ma.Multiaddr, dest peer.ID) bool {
	return g.allowed(src) && g.allowed(dest)
}

// Count adds the bytes each ticketed peer moved since the last count to its account; it returns the peers of the
// accounts that went over their cap (their circuits are cut).
func (g *Gate) Count(totals func(peer.ID) int64) []peer.ID {
	g.mu.Lock()
	defer g.mu.Unlock()
	now := g.now()
	hour := now.Truncate(time.Hour)
	over := map[string]bool{}
	for id, t := range g.peers {
		total := totals(id)
		delta := total - t.seen
		if delta < 0 { // the counter started again
			delta = total
		}
		t.seen = total
		a := g.accounts[t.account]
		if a == nil {
			continue
		}
		if a.window.Before(hour) {
			a.window, a.used = hour, 0
		}
		a.used += delta
		if a.capBytes > 0 && a.used > a.capBytes {
			if now.After(a.overUntil) || now.Equal(a.overUntil) {
				log.Printf("account %s over its %d MB of this hour: refused until %s", t.account, a.capBytes>>20,
					hour.Add(time.Hour).Format(time.RFC3339))
			}
			a.overUntil = hour.Add(time.Hour)
			over[t.account] = true
		}
	}
	var cut []peer.ID
	for id, t := range g.peers {
		if over[t.account] || g.blocked[t.account] || g.banned[id] {
			cut = append(cut, id)
		}
	}
	// Tickets that ended are forgotten (an hour later, so a renewal keeps its count).
	for id, t := range g.peers {
		if now.Sub(t.expires) > time.Hour {
			delete(g.peers, id)
		}
	}
	return cut
}

// Block takes the accounts and the peer keys of the signed blocklist.
func (g *Gate) Block(accounts []string, peerKeys []string) {
	blocked := map[string]bool{}
	for _, a := range accounts {
		blocked[a] = true
	}
	banned := map[peer.ID]bool{}
	for _, k := range peerKeys {
		raw, err := base64.StdEncoding.DecodeString(k)
		if err != nil {
			continue
		}
		pub, err := crypto.UnmarshalEd25519PublicKey(raw)
		if err != nil {
			continue
		}
		if id, err := peer.IDFromPublicKey(pub); err == nil {
			banned[id] = true
		}
	}
	g.mu.Lock()
	g.blocked, g.banned = blocked, banned
	g.mu.Unlock()
}

// Serve answers the ticket protocol on the host.
func (g *Gate) Serve(h host.Host) {
	h.SetStreamHandler(TicketProtocol, func(s network.Stream) {
		defer s.Close()
		_ = s.SetDeadline(time.Now().Add(10 * time.Second))
		line, err := bufio.NewReader(&limited{r: s, left: maxTicketBytes}).ReadString('\n')
		answer := map[string]any{"ok": false}
		if err != nil {
			answer["error"] = "the ticket ends with a new line, in at most 4096 bytes"
		} else if t, err := g.Present(s.Conn().RemotePeer(), strings.TrimSpace(line)); err != nil {
			answer["error"] = err.Error()
		} else {
			answer = map[string]any{"ok": true, "expires": t.Expires}
		}
		out, _ := json.Marshal(answer)
		_, _ = s.Write(append(out, '\n'))
	})
}

type limited struct {
	r    interface{ Read([]byte) (int, error) }
	left int
}

func (l *limited) Read(p []byte) (int, error) {
	if l.left <= 0 {
		return 0, errTooLong
	}
	if len(p) > l.left {
		p = p[:l.left]
	}
	n, err := l.r.Read(p)
	l.left -= n
	return n, err
}

const errTooLong = gateError("too long")

// Meter counts the bytes of the ticketed peers every interval and cuts the connections of the accounts over their cap.
func (g *Gate) Meter(ctx context.Context, h host.Host, counter *metrics.BandwidthCounter, every time.Duration) {
	tick := time.NewTicker(every)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
			for _, p := range g.Count(func(id peer.ID) int64 {
				s := counter.GetBandwidthForPeer(id)
				return s.TotalIn + s.TotalOut
			}) {
				_ = h.Network().ClosePeer(p)
			}
		}
	}
}
