// Command swarm-certs writes the certificates SwarmEval's services authenticate each other
// with: a CA of the deployment's own and one certificate per service. Behavior:
// docs/architecture.md#service-identity.
package main

import (
	"errors"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/jackeydou/DUNE/go/internal/certs"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "swarm-certs:", err)
		os.Exit(1)
	}
}

func run(args []string) error {
	fs := flag.NewFlagSet("swarm-certs", flag.ContinueOnError)
	out := fs.String("out", "", "directory to write to: ca.crt, ca.key, and <service>/{ca.crt,tls.crt,tls.key} (required)")
	newCA := fs.Bool("new-ca", false, "replace the CA in --out, and with it every service certificate. Every service must then be restarted")
	renew := fs.Bool("renew", false, "replace every service certificate, also those with more than 30 days left. Restart the services afterwards")
	var services []string
	fs.Func("service", fmt.Sprintf("issue a certificate for this service only; repeatable. Default: all of %s", strings.Join(certs.Services, ", ")), func(v string) error {
		services = append(services, v)
		return nil
	})
	hosts := map[string][]string{}
	fs.Func("host", fmt.Sprintf("SERVICE=NAME: a DNS name or IP address clients reach the service at, besides its own name and loopback; repeatable. For %s", strings.Join(certs.Servers, ", ")), func(v string) error {
		service, host, ok := strings.Cut(v, "=")
		if !ok || service == "" || host == "" {
			return fmt.Errorf("%q: want SERVICE=NAME, such as control=control.internal", v)
		}
		hosts[service] = append(hosts[service], host)
		return nil
	})
	var public []string
	fs.Func("public-host", "also write public/{tls.crt,tls.key}: a self-signed certificate for edge's public listener, naming this DNS name or IP address; repeatable. One already there is kept while it names the same hosts and has over 30 days left", func(v string) error {
		public = append(public, v)
		return nil
	})
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	if *out == "" {
		return errors.New("--out is required: the directory the certificates are written to")
	}
	written, err := certs.Generate(*out, certs.Options{Services: services, Hosts: hosts, NewCA: *newCA, Renew: *renew, Now: time.Now()})
	if err != nil {
		return err
	}
	if len(written) == 0 {
		fmt.Printf("kept the service certificates in %s: each has more than 30 days left (--renew replaces them)\n", *out)
	} else {
		fmt.Printf("wrote certificates for %s to %s, valid until %s. Restart those services\n", strings.Join(written, ", "), *out, time.Now().Add(certs.LeafValidity).Format(time.DateOnly))
	}
	if len(public) > 0 {
		wrote, err := certs.Public(*out, public, time.Now())
		if err != nil {
			return err
		}
		if wrote {
			fmt.Printf("wrote a self-signed public certificate for %s to %s/%s\n", strings.Join(public, ", "), *out, certs.PublicDir)
		} else {
			fmt.Printf("kept the public certificate in %s/%s\n", *out, certs.PublicDir)
		}
	}
	return nil
}
