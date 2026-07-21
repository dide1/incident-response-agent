package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"testing"
)

func sign(body []byte, secret string) string {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(body)
	return "sha256=" + hex.EncodeToString(mac.Sum(nil))
}

func TestVerifyGitHubSignature_Valid(t *testing.T) {
	body := []byte(`{"action":"completed"}`)
	secret := "mysecret"
	if !verifyGitHubSignature(body, secret, sign(body, secret)) {
		t.Error("valid signature should pass")
	}
}

func TestVerifyGitHubSignature_Invalid(t *testing.T) {
	body := []byte(`{"action":"completed"}`)
	if verifyGitHubSignature(body, "secret", "sha256=badhash") {
		t.Error("invalid signature should fail")
	}
}

func TestVerifyGitHubSignature_NoSecret(t *testing.T) {
	// Empty secret means no verification — any signature passes
	if !verifyGitHubSignature([]byte("anything"), "", "sha256=doesntmatter") {
		t.Error("empty secret should skip verification and return true")
	}
}

func TestVerifyGitHubSignature_TamperedBody(t *testing.T) {
	original := []byte(`{"action":"completed"}`)
	tampered := []byte(`{"action":"deleted"}`)
	secret := "mysecret"
	if verifyGitHubSignature(tampered, secret, sign(original, secret)) {
		t.Error("tampered body should fail signature check")
	}
}
