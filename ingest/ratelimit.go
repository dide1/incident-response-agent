package main

import (
	"sync"
	"time"
)

// rateLimiter implements a per-key token bucket.
// Each repo gets its own bucket; tokens refill at `rate` per second up to `cap`.
// A misbehaving or replaying repo is throttled without affecting others.
type rateLimiter struct {
	mu      sync.Mutex
	buckets map[string]*bucket
	rate    float64 // tokens added per second
	cap     float64 // maximum tokens per bucket
}

type bucket struct {
	mu       sync.Mutex
	tokens   float64
	lastFill time.Time
}

func newRateLimiter(rate, cap float64) *rateLimiter {
	return &rateLimiter{
		buckets: make(map[string]*bucket),
		rate:    rate,
		cap:     cap,
	}
}

// allow returns true and consumes one token if the key has capacity; false otherwise.
func (rl *rateLimiter) allow(key string) bool {
	rl.mu.Lock()
	b, ok := rl.buckets[key]
	if !ok {
		b = &bucket{tokens: rl.cap, lastFill: time.Now()}
		rl.buckets[key] = b
	}
	rl.mu.Unlock()

	b.mu.Lock()
	defer b.mu.Unlock()

	now := time.Now()
	elapsed := now.Sub(b.lastFill).Seconds()
	b.tokens += elapsed * rl.rate
	if b.tokens > rl.cap {
		b.tokens = rl.cap
	}
	b.lastFill = now

	if b.tokens < 1 {
		return false
	}
	b.tokens--
	return true
}
