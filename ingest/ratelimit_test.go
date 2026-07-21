package main

import (
	"testing"
	"time"
)

func TestRateLimiter_AllowsUpToCap(t *testing.T) {
	// cap=3, extremely slow refill (1 token/hour) so tokens don't replenish mid-test
	rl := newRateLimiter(1.0/3600.0, 3)
	for i := range 3 {
		if !rl.allow("myrepo") {
			t.Fatalf("request %d should be allowed (within burst cap)", i+1)
		}
	}
	if rl.allow("myrepo") {
		t.Error("4th request should be denied — burst cap exhausted")
	}
}

func TestRateLimiter_RefillsOverTime(t *testing.T) {
	// 10 tokens/second, cap 1: exhaust the bucket then wait for refill
	rl := newRateLimiter(10, 1)
	rl.allow("repo") // consume the initial token
	time.Sleep(150 * time.Millisecond)
	if !rl.allow("repo") {
		t.Error("should be allowed after tokens have refilled")
	}
}

func TestRateLimiter_IndependentPerRepo(t *testing.T) {
	rl := newRateLimiter(1.0/3600.0, 1)
	rl.allow("repo-a") // exhaust repo-a's bucket
	if !rl.allow("repo-b") {
		t.Error("repo-b should have its own independent bucket")
	}
}
