package cli

import "testing"

func TestCellRendersQueryValues(t *testing.T) {
	for _, c := range []struct {
		value any
		want  string
	}{
		{nil, "NULL"},
		{"plain text", "plain text"},
		{true, "true"},
		{6.0, "6"},
		{-3.0, "-3"},
		{0.25, "0.25"},
		{1e300, "1e+300"},
		{[]any{"a", 1.0}, `["a",1]`},
		{map[string]any{"k": nil}, `{"k":null}`},
	} {
		if got := cell(c.value, "NULL"); got != c.want {
			t.Errorf("cell(%v) = %q, want %q", c.value, got, c.want)
		}
	}
	if got := oneLine("a\nb\tc"); got != `a\nb\tc` {
		t.Errorf("oneLine: %q", got)
	}
}
