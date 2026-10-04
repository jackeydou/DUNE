package edge

import (
	"testing"
	"time"
)

type clock struct{ t time.Time }

func (c *clock) now() time.Time          { return c.t }
func (c *clock) advance(d time.Duration) { c.t = c.t.Add(d) }

func newTestLimiter() (*loginLimiter, *clock) {
	c := &clock{t: time.Date(2026, 10, 3, 12, 0, 0, 0, time.UTC)}
	l := newLoginLimiter()
	l.now = c.now
	return l, c
}

func TestFiveFailuresLockForAMinute(t *testing.T) {
	l, c := newTestLimiter()
	for i := range freeFailures - 1 {
		l.fail("user:ada")
		if w := l.wait("user:ada"); w != 0 {
			t.Fatalf("after %d failures: locked for %s", i+1, w)
		}
	}
	l.fail("user:ada")
	if w := l.wait("user:ada"); w != time.Minute {
		t.Fatalf("after %d failures: wait %s, want 1m", freeFailures, w)
	}
	c.advance(time.Minute)
	if w := l.wait("user:ada"); w != 0 {
		t.Fatalf("after the lock ran out: wait %s", w)
	}
}

func TestEachFurtherFailureDoublesTheLockUpToTheCap(t *testing.T) {
	l, c := newTestLimiter()
	for range freeFailures {
		l.fail("addr:10.0.0.1")
	}
	want := []time.Duration{2 * time.Minute, 4 * time.Minute, 8 * time.Minute, 15 * time.Minute, 15 * time.Minute}
	for _, w := range want {
		c.advance(l.wait("addr:10.0.0.1"))
		l.fail("addr:10.0.0.1")
		if got := l.wait("addr:10.0.0.1"); got != w {
			t.Fatalf("lock %s, want %s", got, w)
		}
	}
}

func TestTheLongestLockAmongKeysCounts(t *testing.T) {
	l, _ := newTestLimiter()
	for range freeFailures {
		l.fail("addr:10.0.0.1")
	}
	if w := l.wait("user:new", "addr:10.0.0.1"); w != time.Minute {
		t.Fatalf("a fresh username from a locked address: wait %s, want 1m", w)
	}
}

func TestSuccessAndQuietTimeStartOver(t *testing.T) {
	l, c := newTestLimiter()
	for range freeFailures - 1 {
		l.fail("user:ada", "user:grace")
	}
	l.succeed("user:ada")
	l.fail("user:ada")
	if w := l.wait("user:ada"); w != 0 {
		t.Fatalf("a sign-in that got through did not clear the count: wait %s", w)
	}
	c.advance(forgetAfter + time.Second)
	l.fail("user:grace")
	if w := l.wait("user:grace"); w != 0 {
		t.Fatalf("failures older than %s still counted: wait %s", forgetAfter, w)
	}
}

func TestForgottenKeysArePruned(t *testing.T) {
	l, c := newTestLimiter()
	for i := range pruneAbove + 1 {
		l.fail("user:" + time.Duration(i).String())
	}
	c.advance(forgetAfter + time.Second)
	l.fail("user:last")
	if n := len(l.entries); n != 1 {
		t.Fatalf("%d keys kept, want only the last", n)
	}
}
