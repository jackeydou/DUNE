package edge

import (
	"testing"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"

	analysisv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/analysis/v1"
	apiv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1"
)

// sameFields fails unless every field of the internal message has a public field with the same
// number, kind, and cardinality, down through nested messages. Names may differ.
func sameFields(t *testing.T, internal, public protoreflect.MessageDescriptor, seen map[protoreflect.FullName]bool) {
	t.Helper()
	if seen[internal.FullName()] {
		return
	}
	seen[internal.FullName()] = true
	fields := internal.Fields()
	if fields.Len() != public.Fields().Len() {
		t.Errorf("%s has %d fields, %s has %d", internal.FullName(), fields.Len(), public.FullName(), public.Fields().Len())
	}
	for i := range fields.Len() {
		in := fields.Get(i)
		out := public.Fields().ByNumber(in.Number())
		if out == nil {
			t.Errorf("%s.%s (field %d) has no public field of that number: edge would drop it", internal.FullName(), in.Name(), in.Number())
			continue
		}
		if in.Kind() != out.Kind() || in.Cardinality() != out.Cardinality() || in.HasPresence() != out.HasPresence() {
			t.Errorf("%s.%s and %s.%s differ in kind, cardinality, or presence", internal.FullName(), in.Name(), public.FullName(), out.Name())
			continue
		}
		if in.Message() != nil && in.Message().FullName() != out.Message().FullName() {
			sameFields(t, in.Message(), out.Message(), seen)
		}
	}
}

func TestAnalysisResponsesKeepEveryField(t *testing.T) {
	for _, pair := range [][2]proto.Message{
		{&analysisv1.QueryResponse{}, &apiv1.QueryResponse{}},
		{&analysisv1.SearchToolCallsResponse{}, &apiv1.SearchToolCallsResponse{}},
		{&analysisv1.StartRuleScanResponse{}, &apiv1.StartRuleScanResponse{}},
		{&analysisv1.GetJobResponse{}, &apiv1.GetJobResponse{}},
		{&analysisv1.JudgeResponse{}, &apiv1.JudgeResponse{}},
		{&analysisv1.ReportResponse{}, &apiv1.ReportResponse{}},
		{&analysisv1.GetTraceResponse{}, &apiv1.GetTraceResponse{}},
		{&analysisv1.DownloadExportResponse{}, &apiv1.DownloadExportResponse{}},
	} {
		sameFields(t, pair[0].ProtoReflect().Descriptor(), pair[1].ProtoReflect().Descriptor(), map[protoreflect.FullName]bool{})
	}
	if int(analysisv1.ExportFormat_EXPORT_FORMAT_EVAL) != int(apiv1.ExportFormat_EXPORT_FORMAT_EVAL) || int(analysisv1.ExportFormat_EXPORT_FORMAT_PARQUET) != int(apiv1.ExportFormat_EXPORT_FORMAT_PARQUET) {
		t.Error("ExportFormat's numbers differ between the internal and the public enum")
	}
}
