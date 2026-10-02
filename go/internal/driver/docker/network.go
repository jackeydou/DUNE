package docker

import (
	"context"
	"fmt"
	"net/netip"

	cerrdefs "github.com/containerd/errdefs"
	"github.com/moby/moby/api/types/network"
	"github.com/moby/moby/client"

	"github.com/jackeydou/DUNE/go/internal/driver"
)

// Bridge options for a sandbox network. inhibit_ipv4 leaves the host without an address on
// the bridge, and so without a route into it; masquerading would only add host rules for
// traffic that never reaches the host. The network must not be internal: docker gives
// containers on an internal network no default route. Verified in
// spec/2026-09-28-runtime-sandbox-logs/q11.
var bridgeOptions = map[string]string{
	"com.docker.network.bridge.inhibit_ipv4":         "true",
	"com.docker.network.bridge.enable_ip_masquerade": "false",
}

func (d *Driver) Subnets(ctx context.Context) ([]netip.Prefix, error) {
	listed, err := d.cli.NetworkList(ctx, client.NetworkListOptions{})
	if err != nil {
		return nil, fmt.Errorf("list networks: %w", err)
	}
	var out []netip.Prefix
	for _, n := range listed.Items {
		for _, c := range n.IPAM.Config {
			if c.Subnet.Addr().Is4() {
				out = append(out, c.Subnet)
			}
		}
	}
	return out, nil
}

func (d *Driver) CreateNetwork(ctx context.Context, spec driver.NetworkSpec) (driver.NetworkID, error) {
	ipv4, ipv6 := true, false
	created, err := d.cli.NetworkCreate(ctx, spec.Name, client.NetworkCreateOptions{
		Driver:     "bridge",
		EnableIPv4: &ipv4,
		EnableIPv6: &ipv6,
		IPAM:       &network.IPAM{Config: []network.IPAMConfig{{Subnet: spec.Subnet, Gateway: spec.Gateway}}},
		Options:    bridgeOptions,
		Labels:     spec.Labels,
	})
	if err != nil {
		return "", fmt.Errorf("create network %s (%s): %w", spec.Name, spec.Subnet, err)
	}
	return driver.NetworkID(created.ID), nil
}

func (d *Driver) ListNetworksByLabels(ctx context.Context, labels map[string]string) ([]driver.NetworkID, error) {
	filters := client.Filters{}
	for k, v := range labels {
		filters.Add("label", k+"="+v)
	}
	listed, err := d.cli.NetworkList(ctx, client.NetworkListOptions{Filters: filters})
	if err != nil {
		return nil, fmt.Errorf("list networks labeled %v: %w", labels, err)
	}
	out := make([]driver.NetworkID, len(listed.Items))
	for i, n := range listed.Items {
		out[i] = driver.NetworkID(n.ID)
	}
	return out, nil
}

func (d *Driver) RemoveNetwork(ctx context.Context, id driver.NetworkID) error {
	_, err := d.cli.NetworkRemove(ctx, string(id), client.NetworkRemoveOptions{})
	if err != nil && !cerrdefs.IsNotFound(err) {
		return fmt.Errorf("remove network %s: %w", id, err)
	}
	return nil
}
