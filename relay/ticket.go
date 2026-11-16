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
// A relay ticket (8 sexies) has no op; an upload ticket (8 nonies) has op "upload", the file and its size.
type Ticket struct {
	Account      string `json:"account"`
	PeerID       string `json:"peer_id"`
	Expires      int64  `json:"expires"`
	CapMBPerHour int64  `json:"cap_mb_per_hour"`
	Op           string `json:"op"`
	SHA256       string `json:"sha256"`
	Size         int64  `json:"size"`
}

func b64url(text string) ([]byte, error) {
	return base64.RawURLEncoding.DecodeString(strings.TrimRight(text, "="))
}

// ParseTicket checks the ticket's signature with the library key, that it is of the peer that presents it and that
// it has not ended. A ticket without a peer (made for HTTPS, 8 octies) or an upload ticket is not one for the relay.
func ParseTicket(text string, libraryKey ed25519.PublicKey, from peer.ID, now time.Time) (*Ticket, error) {
	t, err := signedTicket(text, libraryKey, now)
	if err != nil {
		return nil, err
	}
	if t.Op != "" {
		return nil, errors.New("not a relay ticket")
	}
	if t.PeerID == "" || t.PeerID != from.String() {
		return nil, errors.New("the ticket is of another peer")
	}
	if t.CapMBPerHour <= 0 {
		return nil, errors.New("the ticket gives no traffic")
	}
	return t, nil
}

// ParseBearer checks a ticket presented over HTTPS to the seeder (8 octies, 8 nonies): signed by the library key, not
// ended, and of the use asked ("" to download, "upload" to upload). It is not tied to a peer: whoever holds it may
// use it while it lasts, within its account's cap.
func ParseBearer(text string, libraryKey ed25519.PublicKey, op string, now time.Time) (*Ticket, error) {
	t, err := signedTicket(text, libraryKey, now)
	if err != nil {
		return nil, err
	}
	if t.Op != op {
		return nil, errors.New("a ticket of another use")
	}
	if op == "" && t.CapMBPerHour <= 0 {
		return nil, errors.New("the ticket gives no traffic")
	}
	if op == "upload" && (!sha256Hex.MatchString(t.SHA256) || t.Size <= 0) {
		return nil, errors.New("not an upload ticket")
	}
	return t, nil
}

func signedTicket(text string, libraryKey ed25519.PublicKey, now time.Time) (*Ticket, error) {
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
	if err := json.Unmarshal(payload, &t); err != nil || t.Account == "" {
		return nil, errors.New("not a ticket")
	}
	if now.Unix() >= t.Expires {
		return nil, errors.New("the ticket has ended")
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
