package sandboxd

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"maps"
	"net/netip"
	"os"
	"path/filepath"
	"slices"

	"github.com/jackeydou/DUNE/go/internal/driver"
)

// NetworkMode is how a sandboxd networks its sandboxes.
type NetworkMode string

const (
	// NetworkNone gives each sandbox only a loopback interface. Agents reach the internet only
	// through the worker's web_request tool (trajectory-first spec decision 2).
	NetworkNone NetworkMode = "none"
	// NetworkPerSandbox gives each sandbox its own bridge, on which the host has no address and
	// the gateway address is reserved for net-gateway (runtime spec decision 24). net-gateway is
	// not built, so a sandbox on it reaches nothing. Kept for the network capability.
	NetworkPerSandbox NetworkMode = "per-sandbox"
)

// run is what CreateRun registered: the run's sandboxes, and with NetworkPerSandbox the network
// of each, keyed by sandbox id.
type run struct {
	sandboxes []string
	networks  map[string]runNetwork
}

type runNetwork struct {
	name    string
	gateway netip.Addr
}

// CreateRun registers a run and the sandboxes it will create. With NetworkPerSandbox it also
// creates one network per sandbox; subnets come from cfg.SubnetPool and skip every subnet the
// backend already has, managed or not.
func (s *Service) CreateRun(ctx context.Context, runID string, sandboxIDs []string) error {
	if !runIDPattern.MatchString(runID) {
		return fmt.Errorf("%w: run id %q must match %s", ErrInvalid, runID, runIDPattern)
	}
	if len(sandboxIDs) == 0 {
		return fmt.Errorf("%w: run %s lists no sandboxes", ErrInvalid, runID)
	}
	for i, id := range sandboxIDs {
		if !sandboxIDPattern.MatchString(id) {
			return fmt.Errorf("%w: run %s: sandbox id %q must match %s", ErrInvalid, runID, id, sandboxIDPattern)
		}
		if slices.Contains(sandboxIDs[:i], id) {
			return fmt.Errorf("%w: run %s lists sandbox %s twice", ErrInvalid, runID, id)
		}
	}

	r := &run{sandboxes: slices.Clone(sandboxIDs), networks: map[string]runNetwork{}}
	s.mu.Lock()
	if _, ok := s.runs[runID]; ok {
		s.mu.Unlock()
		return fmt.Errorf("%w: run %s", ErrExists, runID)
	}
	s.runs[runID] = r
	s.mu.Unlock()
	if s.cfg.Network != NetworkPerSandbox {
		return nil
	}

	if err := s.createNetworks(ctx, runID, sandboxIDs, r); err != nil {
		cleanup := s.removeNetworks(context.WithoutCancel(ctx), runID)
		s.mu.Lock()
		delete(s.runs, runID)
		s.mu.Unlock()
		return errors.Join(err, cleanup)
	}
	return nil
}

func (s *Service) createNetworks(ctx context.Context, runID string, sandboxIDs []string, r *run) error {
	// Held across listing and creating, so two runs in this sandboxd never pick one subnet.
	s.netMu.Lock()
	defer s.netMu.Unlock()
	used, err := s.drv.Subnets(ctx)
	if err != nil {
		return err
	}
	for _, id := range sandboxIDs {
		subnet, err := freeSubnet(s.cfg.SubnetPool, s.cfg.SubnetBits, used)
		if err != nil {
			return fmt.Errorf("network for sandbox %s of run %s: %w", id, runID, err)
		}
		used = append(used, subnet)
		n := runNetwork{name: "swarmeval-" + runID + "-" + id, gateway: subnet.Addr().Next()}
		_, err = s.drv.CreateNetwork(ctx, driver.NetworkSpec{
			Name:    n.name,
			Subnet:  subnet,
			Gateway: n.gateway,
			Labels:  map[string]string{driver.LabelManaged: "true", driver.LabelRunID: runID, driver.LabelSandboxID: id},
		})
		if err != nil {
			return fmt.Errorf("sandbox %s of run %s: %w", id, runID, err)
		}
		s.mu.Lock()
		r.networks[id] = n
		s.mu.Unlock()
	}
	return nil
}

// freeSubnet returns the first block of the given size in pool that overlaps nothing in used.
func freeSubnet(pool netip.Prefix, bits int, used []netip.Prefix) (netip.Prefix, error) {
	first := binary.BigEndian.Uint32(pool.Masked().Addr().AsSlice())
	step := uint32(1) << (32 - bits)
	blocks := uint32(1) << (bits - pool.Bits())
	for i := range blocks {
		var a [4]byte
		binary.BigEndian.PutUint32(a[:], first+i*step)
		p := netip.PrefixFrom(netip.AddrFrom4(a), bits)
		if !slices.ContainsFunc(used, p.Overlaps) {
			return p, nil
		}
	}
	return netip.Prefix{}, fmt.Errorf("no free /%d left in the sandbox subnet pool %s. Destroy finished runs, or start sandboxd with a larger --sandbox-subnets", bits, pool)
}

// network returns the network CreateRun made for a sandbox: the zero runNetwork, meaning no
// network at all, unless sandboxd runs with NetworkPerSandbox.
func (s *Service) network(runID, sandboxID string) (runNetwork, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.runs[runID]
	if !ok {
		return runNetwork{}, fmt.Errorf("%w: run %s is not known to this sandboxd. Call CreateRun first; sandboxd forgets runs when it restarts", ErrNotFound, runID)
	}
	if !slices.Contains(r.sandboxes, sandboxID) {
		return runNetwork{}, fmt.Errorf("%w: sandbox %s was not listed when run %s was created. Listed: %v", ErrInvalid, sandboxID, runID, slices.Sorted(slices.Values(r.sandboxes)))
	}
	return r.networks[sandboxID], nil
}

// DestroyRun removes every container and network labeled with the run, including ones this
// process did not create, and the run's state directory.
func (s *Service) DestroyRun(ctx context.Context, runID string) error {
	if !runIDPattern.MatchString(runID) {
		return fmt.Errorf("%w: run id %q", ErrInvalid, runID)
	}
	ids, err := s.drv.ListByLabels(ctx, map[string]string{driver.LabelManaged: "true", driver.LabelRunID: runID})
	if err != nil {
		return err
	}
	var errs []error
	for _, id := range ids {
		errs = append(errs, s.drv.Remove(ctx, id))
	}
	// Networks go after the containers attached to them.
	errs = append(errs, s.removeNetworks(ctx, runID))
	s.mu.Lock()
	maps.DeleteFunc(s.sandboxes, func(k key, _ *sandbox) bool { return k.run == runID })
	delete(s.runs, runID)
	s.mu.Unlock()
	if err := os.RemoveAll(filepath.Join(s.cfg.StateDir, runID)); err != nil {
		errs = append(errs, fmt.Errorf("remove state of run %s: %w", runID, err))
	}
	return errors.Join(errs...)
}

func (s *Service) removeNetworks(ctx context.Context, runID string) error {
	ids, err := s.drv.ListNetworksByLabels(ctx, map[string]string{driver.LabelManaged: "true", driver.LabelRunID: runID})
	if err != nil {
		return err
	}
	var errs []error
	for _, id := range ids {
		errs = append(errs, s.drv.RemoveNetwork(ctx, id))
	}
	return errors.Join(errs...)
}
