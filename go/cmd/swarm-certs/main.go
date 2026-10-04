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
	newCA := fs.Bool("new-ca", false, "replace the CA in --out instead of signing with it. Every service must then be restarted with its new certificate")
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
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	if *out == "" {
		return errors.New("--out is required: the directory the certificates are written to")
	}
	if err := certs.Generate(*out, certs.Options{Services: services, Hosts: hosts, NewCA: *newCA, Now: time.Now()}); err != nil {
		return err
	}
	fmt.Printf("wrote certificates to %s, valid until %s\n", *out, time.Now().Add(certs.LeafValidity).Format(time.DateOnly))
	return nil
}
