// What the seeder checks of a downloaded file (p2p_library.md 3 octies), and ClamAV, run as a program apart (it is
// GPL: called, never linked).
package main

import (
	"archive/zip"
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path"
	"strings"
	"time"
)

const maxUnzipped = 2 << 30

// Executable or script contents and names a 3MF or ZIP may not carry.
var refusedNames = []string{".exe", ".dll", ".so", ".dylib", ".bat", ".cmd", ".com", ".ps1", ".sh", ".vbs", ".js",
	".jar", ".msi", ".scr", ".py", ".lua", ".docm", ".xlsm", ".pptm", ".gcode", ".bgcode", ".gco"}

// kind recognises a file by its content, not by its name: "zip" (3MF), "stl", "obj", "step", "json", "webp",
// "png", "jpeg", or "" when it is none of the allowed types.
func kind(head []byte) string {
	switch {
	case bytes.HasPrefix(head, []byte("PK\x03\x04")):
		return "zip"
	case bytes.HasPrefix(head, []byte("\x89PNG\r\n\x1a\n")):
		return "png"
	case bytes.HasPrefix(head, []byte("\xff\xd8\xff")):
		return "jpeg"
	case len(head) >= 12 && bytes.Equal(head[:4], []byte("RIFF")) && bytes.Equal(head[8:12], []byte("WEBP")):
		return "webp"
	case bytes.HasPrefix(head, []byte("MZ")), bytes.HasPrefix(head, []byte("\x7fELF")), bytes.HasPrefix(head, []byte("#!")):
		return ""
	}
	text := strings.TrimSpace(strings.TrimPrefix(string(head), "\xef\xbb\xbf"))
	switch {
	case strings.HasPrefix(text, "ISO-10303-21"):
		return "step"
	case strings.HasPrefix(text, "{"):
		return "json"
	case strings.HasPrefix(text, "solid"):
		return "stl"
	case strings.HasPrefix(text, "v ") || strings.HasPrefix(text, "#") || strings.HasPrefix(text, "o ") ||
		strings.HasPrefix(text, "mtllib"):
		return "obj"
	}
	if len(head) >= 84 { // binary STL: an 80-byte header and the triangle count
		return "stl"
	}
	return ""
}

// SafeFile is the check of 3 octies: an allowed type by its content and, for a 3MF/ZIP, no path out of its folder,
// no links, no programs inside and at most 10 times its size (and 2 GB) unzipped.
func SafeFile(file string) error {
	f, err := os.Open(file)
	if err != nil {
		return err
	}
	head := make([]byte, 512)
	n, _ := f.Read(head)
	info, _ := f.Stat()
	f.Close()
	k := kind(head[:n])
	if k == "" {
		return errors.New("not one of the allowed types (by its content)")
	}
	if k != "zip" {
		return nil
	}
	z, err := zip.OpenReader(file)
	if err != nil {
		return fmt.Errorf("a damaged 3MF: %v", err)
	}
	defer z.Close()
	var total uint64
	for _, e := range z.File {
		name := e.Name
		clean := path.Clean(strings.ReplaceAll(name, "\\", "/"))
		if path.IsAbs(clean) || strings.HasPrefix(clean, "../") || clean == ".." || strings.Contains(name, ":") {
			return fmt.Errorf("%q goes out of its folder", name)
		}
		if e.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("%q is a link", name)
		}
		lower := strings.ToLower(clean)
		for _, bad := range refusedNames {
			if strings.HasSuffix(lower, bad) {
				return fmt.Errorf("%q is a program, a script or G-code", name)
			}
		}
		total += e.UncompressedSize64
	}
	if total > maxUnzipped || (info != nil && total > 10*uint64(info.Size())+(1<<20)) {
		return fmt.Errorf("unzips to %d bytes: more than 10 times its size or 2 GB", total)
	}
	return nil
}

// ClamAV runs clamdscan (or clamscan) on a file: "" when clean, what it found otherwise. An error: ClamAV did not run.
func ClamAV(ctx context.Context, program, file string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Minute)
	defer cancel()
	out, err := exec.CommandContext(ctx, program, "--no-summary", file).CombinedOutput()
	if err == nil {
		return "", nil
	}
	var exit *exec.ExitError
	if errors.As(err, &exit) && exit.ExitCode() == 1 { // 1: a virus found; 2: an error
		return strings.TrimSpace(string(out)), nil
	}
	return "", fmt.Errorf("%s: %v: %s", program, err, strings.TrimSpace(string(out)))
}
