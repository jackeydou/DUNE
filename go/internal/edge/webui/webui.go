// Package webui serves the console: the single-page app built from console/ and embedded here
// (docs/services/edge.md#console).
package webui

import (
	"embed"
	"io/fs"
	"net/http"
	"path"
	"strings"
)

// static holds the console's build, written by `mise run console:build`. A checkout that has
// not built it holds only a placeholder, and edge then serves the API without the pages.
//
//go:embed all:static
var static embed.FS

// contentSecurityPolicy lets the pages load only what edge itself serves. Styles may be inline
// because the code editor writes its theme into a style element; scripts may not.
const contentSecurityPolicy = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; " +
	"font-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"

// Handler serves the embedded build.
func Handler() http.Handler {
	files, err := fs.Sub(static, "static")
	if err != nil {
		panic(err) // the directory is embedded above; this cannot fail
	}
	return handler(files)
}

// handler serves files as a single-page app: a file that exists is served as it is, and any
// other page path gets index.html, where the app's own router takes over. API paths never get
// a page, so a mistyped procedure is a 404 and not HTML with a 200.
func handler(files fs.FS) http.Handler {
	server := http.FileServerFS(files)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			http.Error(w, "the console's pages are read with GET", http.StatusMethodNotAllowed)
			return
		}
		if strings.HasPrefix(r.URL.Path, "/swarmeval.") {
			http.NotFound(w, r)
			return
		}
		h := w.Header()
		h.Set("Content-Security-Policy", contentSecurityPolicy)
		h.Set("X-Content-Type-Options", "nosniff")
		h.Set("Referrer-Policy", "no-referrer")

		name := strings.TrimPrefix(path.Clean("/"+r.URL.Path), "/")
		if info, err := fs.Stat(files, name); name != "" && err == nil && !info.IsDir() {
			if strings.HasPrefix(name, "assets/") {
				// Vite names assets by content hash, so a name never changes meaning.
				h.Set("Cache-Control", "public, max-age=31536000, immutable")
			}
			server.ServeHTTP(w, r)
			return
		}
		if strings.HasPrefix(name, "assets/") {
			http.NotFound(w, r)
			return
		}
		index, err := fs.ReadFile(files, "index.html")
		if err != nil {
			http.Error(w, "this edge binary was built without the console. Run `mise run console:build`, then build edge again. The API is served either way.", http.StatusNotFound)
			return
		}
		// index.html names the current assets, so it is checked on every load.
		h.Set("Cache-Control", "no-cache")
		h.Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write(index) // a failed write is the reader going away
	})
}
