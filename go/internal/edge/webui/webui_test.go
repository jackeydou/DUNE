package webui

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"testing/fstest"
)

func get(t *testing.T, h http.Handler, method, path string) *httptest.ResponseRecorder {
	t.Helper()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(method, path, nil))
	return rec
}

func TestPagesFallBackToTheAppAndFilesAreServedAsTheyAre(t *testing.T) {
	h := handler(fstest.MapFS{
		"index.html":         {Data: []byte("<html>app</html>")},
		"assets/app-1a2b.js": {Data: []byte("console.log(1)")},
	})

	for _, page := range []string{"/", "/runs", "/cases/safety/demo", "/runs/demo.1a2b.v0.e1"} {
		rec := get(t, h, http.MethodGet, page)
		if rec.Code != http.StatusOK || rec.Body.String() != "<html>app</html>" {
			t.Fatalf("%s: %d %q", page, rec.Code, rec.Body.String())
		}
		if rec.Header().Get("Cache-Control") != "no-cache" || !strings.Contains(rec.Header().Get("Content-Security-Policy"), "frame-ancestors 'none'") || rec.Header().Get("X-Content-Type-Options") != "nosniff" {
			t.Fatalf("%s: headers %v", page, rec.Header())
		}
	}
	asset := get(t, h, http.MethodGet, "/assets/app-1a2b.js")
	if asset.Code != http.StatusOK || asset.Body.String() != "console.log(1)" || !strings.Contains(asset.Header().Get("Cache-Control"), "immutable") {
		t.Fatalf("asset: %d %v", asset.Code, asset.Header())
	}
	if !strings.Contains(asset.Header().Get("Content-Type"), "javascript") {
		t.Fatalf("asset type: %q", asset.Header().Get("Content-Type"))
	}
}

func TestWhatIsNotAPageIsNotAnsweredWithOne(t *testing.T) {
	h := handler(fstest.MapFS{"index.html": {Data: []byte("<html>app</html>")}})

	for path, want := range map[string]int{
		"/swarmeval.api.v1.RunService/NoSuchCall": http.StatusNotFound,
		"/assets/gone-9f9f.js":                    http.StatusNotFound,
		"/../../etc/passwd":                       http.StatusOK, // cleaned to a page path: the app, never a file outside
	} {
		rec := get(t, h, http.MethodGet, path)
		if rec.Code != want || strings.Contains(rec.Body.String(), "root:") {
			t.Errorf("%s: %d %q, want %d", path, rec.Code, rec.Body.String(), want)
		}
	}
	if rec := get(t, h, http.MethodPost, "/runs"); rec.Code != http.StatusMethodNotAllowed {
		t.Errorf("POST /runs: %d", rec.Code)
	}
}

func TestABinaryWithoutTheConsoleSaysSo(t *testing.T) {
	rec := get(t, handler(fstest.MapFS{".gitkeep": {}}), http.MethodGet, "/")
	if rec.Code != http.StatusNotFound || !strings.Contains(rec.Body.String(), "console:build") {
		t.Fatalf("%d %q", rec.Code, rec.Body.String())
	}
}
