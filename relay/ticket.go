// Tickets of the master (tisma_cloud/peers.py) and the library's key they are signed with.
package main

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/libp2p/go-libp2p/core/peer"
)

// CertPrefix is what the project's key signs, followed by the 32 bytes of the library key (p2p_library.md 3 quinquies).
var CertPrefix = []byte("tisma-library-key/1\n")

// Ticket is the payload the master signs with the library key: base64url(payload) "." base64url(signature).
type Ticket struct {
	Account      string `json:"account"`
	PeerID       string `json:"peer_id"`
	Expires      int64  `json:"expires"`
	CapMBPerHour int64  `json:"cap_mb_per_hour"`
}

func b64url(text string) ([]byte, error) {
	return base64.RawURLEncoding.DecodeString(strings.TrimRight(text, "="))
}

// ParseTicket checks the ticket's signature with the library key, that it is of the peer that presents it and that
// it has not ended.
func ParseTicket(text string, libraryKey ed25519.PublicKey, from peer.ID, now time.Time) (*Ticket, error) {
	parts := strings.Split(strings.TrimSpace(text), ".")
	if len(parts) != 2 {
		return nil, errors.New("not a ticket")
	}
	payload, err := b64url(parts[0])
	if err != nil {
		return nil, errors.New("not a ticket")
	}
	signature, err := b64url(parts[1])
	if err != nil || len(signature) != ed25519.SignatureSize {
		return nil, errors.New("not a ticket")
	}
	if !ed25519.Verify(libraryKey, payload, signature) {
		return nil, errors.New("not signed by the library's key")
	}
	var t Ticket
	if err := json.Unmarshal(payload, &t); err != nil || t.Account == "" || t.PeerID == "" {
		return nil, errors.New("not a ticket")
	}
	if t.PeerID != from.String() {
		return nil, errors.New("the ticket is of another peer")
	}
	if now.Unix() >= t.Expires {
		return nil, errors.New("the ticket has ended")
	}
	if t.CapMBPerHour <= 0 {
		return nil, errors.New("the ticket gives no traffic")
	}
	return &t, nil
}

// LibraryKey is the master's library key (GET /v1/library/key), accepted only with a certificate of the project key.
func LibraryKey(publicB64, certificateB64, projectKeyHex string) (ed25519.PublicKey, error) {
	public, err := base64.StdEncoding.DecodeString(publicB64)
	if err != nil || len(public) != ed25519.PublicKeySize {
		return nil, errors.New("the library key is not 32 bytes of base64")
	}
	project, err := hex.DecodeString(strings.TrimSpace(projectKeyHex))
	if err != nil || len(project) != ed25519.PublicKeySize {
		return nil, errors.New("the project key is not 64 hex digits")
	}
	certificate, err := base64.StdEncoding.DecodeString(certificateB64)
	if err != nil || len(certificate) != ed25519.SignatureSize {
		return nil, errors.New("the library key has no certificate")
	}
	if !ed25519.Verify(project, append(append([]byte{}, CertPrefix...), public...), certificate) {
		return nil, fmt.Errorf("the certificate of the library key is not of the project key")
	}
	return public, nil
}
