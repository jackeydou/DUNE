package cli

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"

	"go.yaml.in/yaml/v3"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/structpb"

	apiv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1"
)

// parseOverrides reads `-V axis=values` flags. The values are read as a YAML flow sequence, as
// `report --compare` reads them: `framing=a,b` is ["a", "b"], `n=1,2` is [1, 2], and
// `paraphrased=[],[dm_ab]` is [[], ["dm_ab"]]. The control plane checks them against the case.
func parseOverrides(flags []string) (*structpb.Struct, error) {
	axes := map[string]any{}
	for _, f := range flags {
		axis, values, ok := strings.Cut(f, "=")
		if !ok || axis == "" || values == "" {
			return nil, fmt.Errorf("-V %q: want axis=value[,value…], such as -V framing=neutral,pressure", f)
		}
		if _, dup := axes[axis]; dup {
			return nil, fmt.Errorf("-V gives axis %q twice; list all its values in one flag, comma-separated", axis)
		}
		var list []any
		if err := yaml.Unmarshal([]byte("["+values+"]"), &list); err != nil {
			return nil, fmt.Errorf("-V %s: the values %q are not a YAML flow sequence: %w", axis, values, err)
		}
		axes[axis] = list
	}
	overrides, err := structpb.NewStruct(axes)
	if err != nil {
		return nil, fmt.Errorf("-V: %w", err)
	}
	return overrides, nil
}

// defaultSlot is the model slot of the agents that name none (docs/case-format.md#model-slots).
const defaultSlot = "default"

// parseModels reads `swarm run`'s -m flags: MODEL[,MODEL…] for the default slot, or
// SLOT=MODEL[,MODEL…] for a named one.
func parseModels(flags []string) (map[string]*apiv1.ModelChoice, error) {
	models := map[string]*apiv1.ModelChoice{}
	for _, f := range flags {
		slot, names, named := strings.Cut(f, "=")
		if !named {
			slot, names = defaultSlot, f
		}
		list := strings.Split(names, ",")
		if slot == "" || slices.Contains(list, "") {
			return nil, fmt.Errorf("-m %q: want MODEL[,MODEL…] for the default slot or SLOT=MODEL[,MODEL…], such as -m qwen3-8b,glm-5 or -m attacker=glm-5", f)
		}
		if _, dup := models[slot]; dup {
			return nil, fmt.Errorf("-m gives slot %q twice; list all its models in one flag, comma-separated", slot)
		}
		models[slot] = &apiv1.ModelChoice{Names: list}
	}
	return models, nil
}

// modelEdits reads `swarm runs replay`'s -m flags: [SLOT=]MODEL, one model per slot, as the
// fork's model replacements.
func modelEdits(flags []string) ([]*apiv1.ForkEdit, error) {
	seen := map[string]bool{}
	edits := make([]*apiv1.ForkEdit, 0, len(flags))
	for _, f := range flags {
		slot, model, named := strings.Cut(f, "=")
		if !named {
			slot, model = defaultSlot, f
		}
		if slot == "" || model == "" || strings.Contains(model, ",") {
			return nil, fmt.Errorf("-m %q: want MODEL for the default slot or SLOT=MODEL, one model per slot", f)
		}
		if seen[slot] {
			return nil, fmt.Errorf("-m gives slot %q twice; a fork runs each slot on one model", slot)
		}
		seen[slot] = true
		edits = append(edits, &apiv1.ForkEdit{Edit: &apiv1.ForkEdit_ReplaceModel{
			ReplaceModel: &apiv1.ReplaceModel{Slot: slot, Model: model},
		}})
	}
	return edits, nil
}

// suiteBundles reads a suite file and packs every case directory its `cases[].path` names,
// relative to the file. Everything else in the suite is the control plane's to check.
func suiteBundles(path string) (string, map[string][]byte, error) {
	text, err := os.ReadFile(path)
	if err != nil {
		return "", nil, fmt.Errorf("read suite %s: %w", path, err)
	}
	var suite struct {
		Cases []struct {
			Path string `yaml:"path"`
		} `yaml:"cases"`
	}
	if err := yaml.Unmarshal(text, &suite); err != nil {
		return "", nil, fmt.Errorf("suite %s is not valid YAML: %w", path, err)
	}
	if len(suite.Cases) == 0 {
		return "", nil, fmt.Errorf("suite %s lists no `cases`", path)
	}
	bundles := map[string][]byte{}
	total := len(text)
	for i, c := range suite.Cases {
		if c.Path == "" {
			return "", nil, fmt.Errorf("suite %s `cases[%d]` has no `path`", path, i)
		}
		if _, done := bundles[c.Path]; done {
			continue
		}
		bundle, err := pack(filepath.Join(filepath.Dir(path), filepath.FromSlash(c.Path)))
		if err != nil {
			return "", nil, fmt.Errorf("suite %s `cases[%d]`: %w", path, i, err)
		}
		bundles[c.Path] = bundle
		total += len(bundle)
		if total > maxBundleBytes {
			return "", nil, fmt.Errorf("suite %s and its cases are over %d bytes (64 MiB) packed; split the suite", path, maxBundleBytes)
		}
	}
	return string(text), bundles, nil
}

// readEdits reads a fork's edits: a YAML or JSON list whose items are ForkEdit in protobuf's
// JSON form, such as `{replace_delivery: {send_event_id: e3, recipient: b, content: hi}}`.
func readEdits(path string) ([]*apiv1.ForkEdit, error) {
	text, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read edits %s: %w", path, err)
	}
	var items []any
	if err := yaml.Unmarshal(text, &items); err != nil {
		return nil, fmt.Errorf("edits %s: want a YAML or JSON list of edits: %w", path, err)
	}
	edits := make([]*apiv1.ForkEdit, len(items))
	for i, item := range items {
		raw, err := json.Marshal(item)
		if err != nil {
			return nil, fmt.Errorf("edits %s item %d: %w", path, i, err)
		}
		edits[i] = &apiv1.ForkEdit{}
		if err := protojson.Unmarshal(raw, edits[i]); err != nil {
			return nil, fmt.Errorf("edits %s item %d: %w. Each item is one of replace_message {agent_id, index, content}, delete_message {agent_id, index}, or replace_delivery {send_event_id, recipient, content}; replace models with -m", path, i, err)
		}
	}
	return edits, nil
}
