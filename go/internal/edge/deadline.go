package edge

import (
	"log/slog"
	"net/http"
	"time"

	"github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1/apiv1connect"
)

// uploadProcedures carry case bundles, so their bodies get UploadTimeout.
var uploadProcedures = map[string]bool{
	apiv1connect.RunServiceSubmitRunsProcedure: true,
}

// withBodyDeadline sets a read deadline on the request's connection (its stream, under HTTP/2)
// before the body is read, so a client cannot hold a connection by dripping a body.
// The deadline does not reach past the body: a response, an event stream included, may take as
// long as it takes (TestAStreamOutlivesTheBodyDeadline, over both protocols).
func withBodyDeadline(next http.Handler, cfg Config, log *slog.Logger) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		limit := cfg.BodyTimeout
		if uploadProcedures[r.URL.Path] {
			limit = cfg.UploadTimeout
		}
		if err := http.NewResponseController(w).SetReadDeadline(time.Now().Add(limit)); err != nil {
			log.Error("set a request body deadline", "path", r.URL.Path, "err", err)
			http.Error(w, "edge cannot bound this request's body; see the operator's log", http.StatusInternalServerError)
			return
		}
		next.ServeHTTP(w, r)
	})
}
