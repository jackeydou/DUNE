package sandboxd

import (
	"context"
	"errors"
	"io/fs"
	"log/slog"
	"math"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/durationpb"

	"github.com/jackeydou/DUNE/go/internal/driver"
	"github.com/jackeydou/DUNE/go/internal/fsdiff"
	sandboxv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/sandbox/v1"
)

// chunkSize keeps every stream message well under gRPC's default 4 MiB limit.
const chunkSize = 1 << 20

// Server adapts Service to sandboxv1.SandboxServiceServer.
type Server struct {
	sandboxv1.UnimplementedSandboxServiceServer
	svc *Service
	log *slog.Logger
}

// NewServer wraps svc.
func NewServer(svc *Service, log *slog.Logger) *Server {
	return &Server{svc: svc, log: log}
}

func (s *Server) CreateRun(ctx context.Context, req *sandboxv1.CreateRunRequest) (*sandboxv1.CreateRunResponse, error) {
	if err := s.svc.CreateRun(ctx, req.GetRunId(), req.GetSandboxIds()); err != nil {
		return nil, s.status("CreateRun", err)
	}
	return &sandboxv1.CreateRunResponse{}, nil
}

func (s *Server) CreateSandbox(ctx context.Context, req *sandboxv1.CreateSandboxRequest) (*sandboxv1.CreateSandboxResponse, error) {
	res := req.GetResources()
	if res.GetCpus() < 0 || res.GetMemoryBytes() < 0 || res.GetPids() < 0 || res.GetDiskBytes() < 0 {
		return nil, status.Errorf(codes.InvalidArgument, "sandbox %s of run %s: resource limits must not be negative, got %v", req.GetSandboxId(), req.GetRunId(), res)
	}
	mounts := make([]Mount, len(req.GetMounts()))
	for i, m := range req.GetMounts() {
		mounts[i] = Mount{Path: m.GetPath(), ReadOnly: m.GetReadOnly(), Protected: m.GetProtected()}
	}
	files := make([]SeedFile, len(req.GetFiles()))
	for i, f := range req.GetFiles() {
		files[i] = SeedFile{Path: f.GetPath(), Content: f.GetContent(), Mode: fs.FileMode(f.GetMode()).Perm()}
	}
	runtime, err := s.svc.CreateSandbox(ctx, CreateRequest{
		RunID:     req.GetRunId(),
		SandboxID: req.GetSandboxId(),
		Image:     req.GetImage(),
		Mounts:    mounts,
		Resources: driver.Resources{
			NanoCPUs:    int64(math.Round(res.GetCpus() * 1e9)),
			MemoryBytes: res.GetMemoryBytes(),
			Pids:        res.GetPids(),
			DiskBytes:   res.GetDiskBytes(),
		},
		Files:     files,
		Users:     req.GetUsers(),
		Env:       req.GetEnv(),
		Hostname:  req.GetHostname(),
		MachineID: req.GetMachineId(),
		Display:   display(req.GetDisplay()),
	})
	if err != nil {
		return nil, s.status("CreateSandbox", err)
	}
	return &sandboxv1.CreateSandboxResponse{Runtime: runtime}, nil
}

func (s *Server) Exec(req *sandboxv1.ExecRequest, stream grpc.ServerStreamingServer[sandboxv1.ExecResponse]) error {
	if err := req.GetTimeout().CheckValid(); err != nil {
		return status.Errorf(codes.InvalidArgument, "call %s: timeout: %v", req.GetCallId(), err)
	}
	res, err := s.svc.Exec(stream.Context(), ExecRequest{
		RunID:     req.GetRunId(),
		SandboxID: req.GetSandboxId(),
		CallID:    req.GetCallId(),
		Argv:      req.GetArgv(),
		Cwd:       req.GetCwd(),
		User:      req.GetUser(),
		Timeout:   req.GetTimeout().AsDuration(),
		Collect:   req.GetCollect(),
	})
	if err != nil {
		return s.status("Exec", err)
	}
	header := &sandboxv1.ExecHeader{
		ExitCode:          int32(res.ExitCode),
		TimedOut:          res.TimedOut,
		Duration:          durationpb.New(res.Duration),
		Stdout:            output(res.Stdout),
		Stderr:            output(res.Stderr),
		BackgroundChanges: changes(res.Background),
		Changes:           changes(res.Changes),
		Processes:         processes(res.Processes),
		Collected:         collected(res.Collected),
	}
	if err := stream.Send(&sandboxv1.ExecResponse{Item: &sandboxv1.ExecResponse_Header{Header: header}}); err != nil {
		return err
	}
	return sendBlobs(res.Blobs, func(c *sandboxv1.BlobChunk) error {
		return stream.Send(&sandboxv1.ExecResponse{Item: &sandboxv1.ExecResponse_Blob{Blob: c}})
	})
}

func (s *Server) RestoreFiles(ctx context.Context, req *sandboxv1.RestoreFilesRequest) (*sandboxv1.RestoreFilesResponse, error) {
	dirs := make([]RestoreDir, len(req.GetDirs()))
	for i, d := range req.GetDirs() {
		dirs[i] = RestoreDir{Path: d.GetPath(), Mode: fs.FileMode(d.GetMode()).Perm(), UID: d.GetUid()}
	}
	files := make([]RestoreFile, len(req.GetFiles()))
	for i, f := range req.GetFiles() {
		files[i] = RestoreFile{Path: f.GetPath(), Content: f.GetContent(), Mode: fs.FileMode(f.GetMode()).Perm(), UID: f.GetUid()}
	}
	unowned, err := s.svc.RestoreFiles(ctx, RestoreRequest{
		RunID:     req.GetRunId(),
		SandboxID: req.GetSandboxId(),
		Remove:    req.GetRemove(),
		Dirs:      dirs,
		Files:     files,
	})
	if err != nil {
		return nil, s.status("RestoreFiles", err)
	}
	return &sandboxv1.RestoreFilesResponse{Unowned: unowned}, nil
}

func (s *Server) ReadFile(ctx context.Context, req *sandboxv1.ReadFileRequest) (*sandboxv1.ReadFileResponse, error) {
	content, size, err := s.svc.ReadFile(ctx, req.GetRunId(), req.GetSandboxId(), req.GetPath(), req.GetMaxBytes())
	if err != nil {
		return nil, s.status("ReadFile", err)
	}
	return &sandboxv1.ReadFileResponse{Content: content, Size: size, Truncated: size > int64(len(content))}, nil
}

func (s *Server) FinalDiff(req *sandboxv1.FinalDiffRequest, stream grpc.ServerStreamingServer[sandboxv1.FinalDiffResponse]) error {
	sandboxes, blobs, err := s.svc.FinalDiff(stream.Context(), req.GetRunId())
	if err != nil {
		return s.status("FinalDiff", err)
	}
	for _, sc := range sandboxes {
		item := &sandboxv1.SandboxChanges{SandboxId: sc.SandboxID, Changes: changes(sc.Changes)}
		if err := stream.Send(&sandboxv1.FinalDiffResponse{Item: &sandboxv1.FinalDiffResponse_Changes{Changes: item}}); err != nil {
			return err
		}
	}
	return sendBlobs(blobs, func(c *sandboxv1.BlobChunk) error {
		return stream.Send(&sandboxv1.FinalDiffResponse{Item: &sandboxv1.FinalDiffResponse_Blob{Blob: c}})
	})
}

func (s *Server) DestroyRun(ctx context.Context, req *sandboxv1.DestroyRunRequest) (*sandboxv1.DestroyRunResponse, error) {
	if err := s.svc.DestroyRun(ctx, req.GetRunId()); err != nil {
		return nil, s.status("DestroyRun", err)
	}
	return &sandboxv1.DestroyRunResponse{}, nil
}

// status maps a service error to a gRPC status. Unexpected errors are logged here, once.
func (s *Server) status(rpc string, err error) error {
	code := codes.Internal
	switch {
	case errors.Is(err, ErrInvalid):
		code = codes.InvalidArgument
	case errors.Is(err, ErrNotFound):
		code = codes.NotFound
	case errors.Is(err, ErrExists):
		code = codes.AlreadyExists
	case errors.Is(err, ErrState):
		code = codes.FailedPrecondition
	case errors.Is(err, context.Canceled):
		code = codes.Canceled
	case errors.Is(err, context.DeadlineExceeded):
		code = codes.DeadlineExceeded
	default:
		s.log.Error("rpc failed", "rpc", rpc, "err", err)
	}
	return status.Error(code, err.Error())
}

func sendBlobs(blobs []Blob, send func(*sandboxv1.BlobChunk) error) error {
	for _, b := range blobs {
		for off := 0; ; off += chunkSize {
			end := min(off+chunkSize, len(b.Data))
			last := end == len(b.Data)
			if err := send(&sandboxv1.BlobChunk{Sha256: b.SHA256, Data: b.Data[off:end], Last: last}); err != nil {
				return err
			}
			if last {
				break
			}
		}
	}
	return nil
}

func display(d *sandboxv1.Display) *Display {
	if d == nil {
		return nil
	}
	return &Display{Width: d.GetWidth(), Height: d.GetHeight(), URL: d.GetUrl()}
}

func collected(cs []Collected) []*sandboxv1.CollectedFile {
	out := make([]*sandboxv1.CollectedFile, len(cs))
	for i, c := range cs {
		out[i] = &sandboxv1.CollectedFile{Path: c.Path, Missing: c.Missing, Size: c.Size, Sha256: c.SHA256}
	}
	return out
}

func output(o Output) *sandboxv1.Output {
	return &sandboxv1.Output{Inline: o.Inline, Size: o.Size, BlobSha256: o.BlobSHA256, Capped: o.Capped}
}

func processes(ps []driver.Process) []*sandboxv1.Process {
	out := make([]*sandboxv1.Process, len(ps))
	for i, p := range ps {
		out[i] = &sandboxv1.Process{Pid: p.PID, Ppid: p.PPID, User: p.User, Cmdline: p.Cmdline}
	}
	return out
}

func changes(cs []Change) []*sandboxv1.FsChange {
	out := make([]*sandboxv1.FsChange, len(cs))
	for i, c := range cs {
		current := c.After
		if c.Op == fsdiff.OpDelete {
			current = c.Before
		}
		attribution := sandboxv1.FsChange_ATTRIBUTION_CALL
		if c.Ambiguous {
			attribution = sandboxv1.FsChange_ATTRIBUTION_AMBIGUOUS
		}
		out[i] = &sandboxv1.FsChange{
			Path:           c.Path,
			Op:             ops[c.Op],
			Kind:           kinds[current.Kind],
			Uid:            current.UID,
			Mode:           unixMode(current.Mode),
			Size:           current.Size,
			BeforeSha256:   c.Before.SHA256,
			AfterSha256:    c.After.SHA256,
			Protected:      c.Protected,
			Attribution:    attribution,
			CandidateCalls: c.Candidates,
			Content:        c.Content,
			MtimeNs:        current.MtimeNs,
		}
	}
	return out
}

var ops = map[fsdiff.Op]sandboxv1.FsChange_Op{
	fsdiff.OpCreate: sandboxv1.FsChange_OP_CREATE,
	fsdiff.OpModify: sandboxv1.FsChange_OP_MODIFY,
	fsdiff.OpDelete: sandboxv1.FsChange_OP_DELETE,
}

var kinds = map[fsdiff.Kind]sandboxv1.FsChange_Kind{
	fsdiff.KindFile:    sandboxv1.FsChange_KIND_FILE,
	fsdiff.KindDir:     sandboxv1.FsChange_KIND_DIR,
	fsdiff.KindSymlink: sandboxv1.FsChange_KIND_SYMLINK,
	fsdiff.KindOther:   sandboxv1.FsChange_KIND_OTHER,
}

// unixMode gives permission bits the way chmod writes them; Go's fs.FileMode keeps the
// special bits elsewhere.
func unixMode(m fs.FileMode) uint32 {
	out := uint32(m.Perm())
	if m&fs.ModeSetuid != 0 {
		out |= 0o4000
	}
	if m&fs.ModeSetgid != 0 {
		out |= 0o2000
	}
	if m&fs.ModeSticky != 0 {
		out |= 0o1000
	}
	return out
}
