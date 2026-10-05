// Package cli is the `swarm` command line: a client of edge's public API and nothing more
// (docs/services/edge.md#swarm-cli).
package cli

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"

	"go.yaml.in/yaml/v3"
)

// Config is where the CLI finds edge and how it signs in. The file holds a credential, so it is
// written readable by its owner only.
type Config struct {
	Endpoint string `yaml:"endpoint"`
	Token    string `yaml:"token,omitempty"`
	// TokenID is the token's public id when `swarm login` made it, so `swarm logout` can revoke
	// it. Empty for a token given with --token.
	TokenID string `yaml:"token_id,omitempty"`
	// CAFile is a PEM file of certificates to trust for edge besides the system's: the
	// certificate of an edge that serves a self-signed one, or the CA that signed it.
	CAFile string `yaml:"ca_file,omitempty"`
}

// Environment variables that override the file.
const (
	envConfig   = "SWARM_CONFIG"
	envEndpoint = "SWARM_ENDPOINT"
	envToken    = "SWARM_TOKEN"
	envCAFile   = "SWARM_CA_FILE"
)

// configPath is $SWARM_CONFIG, else $XDG_CONFIG_HOME/swarm/config.yaml, else
// ~/.config/swarm/config.yaml.
func configPath() (string, error) {
	if p := os.Getenv(envConfig); p != "" {
		return p, nil
	}
	if dir := os.Getenv("XDG_CONFIG_HOME"); dir != "" {
		return filepath.Join(dir, "swarm", "config.yaml"), nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("find the config file: %w; set %s", err, envConfig)
	}
	return filepath.Join(home, ".config", "swarm", "config.yaml"), nil
}

// loadConfig reads the file, if there is one, and applies the environment over it.
func loadConfig() (Config, string, error) {
	path, err := configPath()
	if err != nil {
		return Config{}, "", err
	}
	var cfg Config
	data, err := os.ReadFile(path)
	switch {
	case errors.Is(err, fs.ErrNotExist):
	case err != nil:
		return Config{}, path, fmt.Errorf("read %s: %w", path, err)
	default:
		if err := yaml.Unmarshal(data, &cfg); err != nil {
			return Config{}, path, fmt.Errorf("%s is not valid YAML: %w. Fix it, or delete it and run `swarm login`", path, err)
		}
	}
	if v := os.Getenv(envEndpoint); v != "" {
		cfg.Endpoint = v
	}
	if v := os.Getenv(envToken); v != "" {
		cfg.Token, cfg.TokenID = v, ""
	}
	if v := os.Getenv(envCAFile); v != "" {
		cfg.CAFile = v
	}
	return cfg, path, nil
}

func saveConfig(path string, cfg Config) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return fmt.Errorf("create %s: %w", filepath.Dir(path), err)
	}
	data, err := yaml.Marshal(cfg)
	if err != nil {
		return err
	}
	// Write then rename, so a crash never leaves a half-written credential file.
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return fmt.Errorf("write %s: %w", tmp, err)
	}
	if err := os.Rename(tmp, path); err != nil {
		return fmt.Errorf("replace %s: %w", path, err)
	}
	return nil
}
