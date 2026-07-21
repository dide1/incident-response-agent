package main

import (
	"testing"
	"time"
)

func TestDedupCache_NewIDNotSeen(t *testing.T) {
	c := newDedupCache(1 * time.Minute)
	if c.seen("abc-123") {
		t.Error("new ID should not be seen")
	}
}

func TestDedupCache_SameIDSeenTwice(t *testing.T) {
	c := newDedupCache(1 * time.Minute)
	c.seen("abc-123")
	if !c.seen("abc-123") {
		t.Error("same ID should be seen on second call")
	}
}

func TestDedupCache_ExpiredIDForgotten(t *testing.T) {
	c := newDedupCache(50 * time.Millisecond)
	c.seen("abc-123")
	time.Sleep(200 * time.Millisecond) // wait for TTL + sweep cycle
	if c.seen("abc-123") {
		t.Error("expired ID should be forgotten after TTL")
	}
}

func TestDedupCache_DifferentIDsAreIndependent(t *testing.T) {
	c := newDedupCache(1 * time.Minute)
	c.seen("id-1")
	if c.seen("id-2") {
		t.Error("different IDs should be tracked independently")
	}
}
