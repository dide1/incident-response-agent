package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
)

// verifyGitHubSignature checks X-Hub-Signature-256 against the raw request body.
// Returns true when no secret is configured (verification skipped).
func verifyGitHubSignature(body []byte, secret, signature string) bool {
	if secret == "" {
		return true
	}
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(body)
	expected := "sha256=" + hex.EncodeToString(mac.Sum(nil))
	return hmac.Equal([]byte(expected), []byte(signature))
}
