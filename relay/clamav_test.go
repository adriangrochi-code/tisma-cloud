package main

import (
	"archive/zip"
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The EICAR test file (https://www.eicar.org, «Download Anti Malware Testfile»): every antivirus reports it, and it is
// harmless.
const eicar = `X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*`

// TestTheRealClamAV runs ClamAV itself (TISMA_CLAMSCAN: clamscan or clamdscan, with its signatures) on the EICAR file,
// on a 3MF that carries it, and on a clean 3MF. The CI installs it in the relay job.
func TestTheRealClamAV(t *testing.T) {
	program := os.Getenv("TISMA_CLAMSCAN")
	if program == "" {
		t.Skip("set TISMA_CLAMSCAN to clamscan")
	}
	dir := t.TempDir()
	write3MF := func(name string, files map[string]string) string {
		p := filepath.Join(dir, name)
		f, err := os.Create(p)
		if err != nil {
			t.Fatal(err)
		}
		z := zip.NewWriter(f)
		for n, body := range files {
			w, _ := z.Create(n)
			_, _ = w.Write([]byte(body))
		}
		if err := z.Close(); err != nil {
			t.Fatal(err)
		}
		f.Close()
		return p
	}
	bare := filepath.Join(dir, "eicar.com.txt")
	if err := os.WriteFile(bare, []byte(eicar), 0o644); err != nil {
		t.Fatal(err)
	}
	inside := write3MF("infected.3mf", map[string]string{"3D/3dmodel.model": "<model/>", "Metadata/notes.txt": eicar})
	clean := write3MF("clean.3mf", map[string]string{"3D/3dmodel.model": "<model/>", "Metadata/notes.txt": "a bar"})
	for _, file := range []string{bare, inside} {
		found, err := ClamAV(context.Background(), program, file)
		if err != nil {
			t.Fatalf("%s: ClamAV did not run: %v", filepath.Base(file), err)
		}
		if !strings.Contains(found, "FOUND") {
			t.Fatalf("%s: nothing found: %q", filepath.Base(file), found)
		}
		t.Logf("%s: %s", filepath.Base(file), found)
	}
	if found, err := ClamAV(context.Background(), program, clean); err != nil || found != "" {
		t.Fatalf("clean.3mf: %q %v", found, err)
	}
	// A file ClamAV cannot read is an error (ClamAV did not run on it), never «clean».
	if _, err := ClamAV(context.Background(), program, filepath.Join(dir, "missing.3mf")); err == nil {
		t.Fatal("a missing file passed as clean")
	}
}
