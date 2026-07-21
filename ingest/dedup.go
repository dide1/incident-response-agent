package main

import (
	"sync"
	"time"
)

// dedupCache tracks recently seen webhook delivery IDs with a TTL.
// GitHub retries webhook deliveries on non-2xx responses; this cache drops
// duplicates at the ingestion layer before they reach the Python backend.
type dedupCache struct {
	mu      sync.Mutex
	entries map[string]time.Time
	ttl     time.Duration
}

func newDedupCache(ttl time.Duration) *dedupCache {
	c := &dedupCache{
		entries: make(map[string]time.Time),
		ttl:     ttl,
	}
	go c.sweep()
	return c
}

// seen returns true if id was already recorded; otherwise records it and returns false.
func (c *dedupCache) seen(id string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, ok := c.entries[id]; ok {
		return true
	}
	c.entries[id] = time.Now()
	return false
}

// sweep runs in a goroutine, periodically deleting entries older than ttl.
func (c *dedupCache) sweep() {
	ticker := time.NewTicker(c.ttl / 2)
	defer ticker.Stop()
	for range ticker.C {
		c.mu.Lock()
		cutoff := time.Now().Add(-c.ttl)
		for id, t := range c.entries {
			if t.Before(cutoff) {
				delete(c.entries, id)
			}
		}
		c.mu.Unlock()
	}
}
