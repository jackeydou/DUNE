package edge

import (
	"sync"
	"time"
)

// Sign-in throttling: after freeFailures failures in a row a key is locked for firstLock, and
// each failure after that doubles the lock, up to maxLock. A key with no failure for forgetAfter
// past its lock starts over. Keys are a username and a client address, so one attacker cannot
// spread guesses over many usernames, and many addresses cannot spread them over one.
const (
	freeFailures = 5
	firstLock    = time.Minute
	maxLock      = 15 * time.Minute
	forgetAfter  = 15 * time.Minute
	// pruneAbove bounds memory: past this many keys, a failure first drops the forgotten ones.
	pruneAbove = 10_000
)

type attempts struct {
	failures    int
	lockedUntil time.Time
	last        time.Time
}

// loginLimiter holds its counts in memory, so they start over when edge restarts and are not
// shared between replicas.
type loginLimiter struct {
	mu      sync.Mutex
	now     func() time.Time
	entries map[string]*attempts
}

func newLoginLimiter() *loginLimiter {
	return &loginLimiter{now: time.Now, entries: map[string]*attempts{}}
}

// wait returns how long the longest lock among keys still runs; zero when none is locked.
func (l *loginLimiter) wait(keys ...string) time.Duration {
	l.mu.Lock()
	defer l.mu.Unlock()
	now := l.now()
	var longest time.Duration
	for _, k := range keys {
		if a := l.entries[k]; a != nil && a.lockedUntil.After(now) {
			longest = max(longest, a.lockedUntil.Sub(now))
		}
	}
	return longest
}

func (l *loginLimiter) fail(keys ...string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	now := l.now()
	if len(l.entries) > pruneAbove {
		for k, a := range l.entries {
			if forgotten(a, now) {
				delete(l.entries, k)
			}
		}
	}
	for _, k := range keys {
		a := l.entries[k]
		if a == nil || forgotten(a, now) {
			a = &attempts{}
			l.entries[k] = a
		}
		a.failures++
		a.last = now
		if over := a.failures - freeFailures; over >= 0 {
			lock := firstLock << min(over, 10)
			a.lockedUntil = now.Add(min(lock, maxLock))
		}
	}
}

// succeed clears key's count, after a sign-in that got through.
func (l *loginLimiter) succeed(key string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	delete(l.entries, key)
}

func forgotten(a *attempts, now time.Time) bool {
	quiet := a.last
	if a.lockedUntil.After(quiet) {
		quiet = a.lockedUntil
	}
	return now.Sub(quiet) > forgetAfter
}
