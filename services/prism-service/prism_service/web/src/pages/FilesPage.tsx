import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, SectionLabel, Empty } from "@/components/ui";

// Mirrors services/document_tree.classify()'s output shape.
type FolderNode = {
  path: string;
  kind: "area" | "series" | "name" | "date";
  doc_count: number;
  children: FolderNode[];
};

type DocumentsResponse = {
  folders: FolderNode[];
  loose_in_root: string[];
  date_format_breaks: string[];
};

const EMPTY: DocumentsResponse = { folders: [], loose_in_root: [], date_format_breaks: [] };

// The one prototype style this surface brings forward (task 5bfdf527):
// a dim uppercase kind label (area/series/name/date) next to each folder.
// Everything else below is PRISM's existing Card/SectionLabel/Empty chrome
// and PRISM's own tokens/fonts — no other prototype styling rides along.
const KIND_LABEL_STYLE = `
.files-kind-label {
  font-size: .65em;
  text-transform: uppercase;
  letter-spacing: .1em;
  color: var(--text-muted);
}
`;

function leafName(path: string): string {
  const parts = path.split("/");
  return parts[parts.length - 1] || path;
}

function FolderRow({ node, depth }: { node: FolderNode; depth: number }) {
  return (
    <div>
      <div
        className="flex items-center gap-2 py-1.5 text-sm border-b border-[color:var(--border-default)]/20"
        style={{ paddingLeft: `${depth * 16}px` }}
      >
        <span className="text-[color:var(--text-primary)] truncate">{leafName(node.path)}</span>
        <span className="files-kind-label">{node.kind}</span>
        <span className="ml-auto text-xs font-mono opacity-60">{node.doc_count}</span>
      </div>
      {node.children.map((child) => (
        <FolderRow key={child.path} node={child} depth={depth + 1} />
      ))}
    </div>
  );
}

export default function FilesPage() {
  const [project] = useProject();
  const [data, setData] = useState<DocumentsResponse>(EMPTY);

  useEffect(() => {
    api
      .get<DocumentsResponse>(`/api/documents?project=${encodeURIComponent(project)}`)
      .then((d) => setData({
        folders: d.folders ?? [],
        loose_in_root: d.loose_in_root ?? [],
        date_format_breaks: d.date_format_breaks ?? [],
      }))
      .catch(() => setData(EMPTY));
  }, [project]);

  return (
    <Page>
      <style>{KIND_LABEL_STYLE}</style>
      <div>
        <div className="text-lg font-semibold text-[color:var(--text-primary)]">Files</div>
        <div className="text-[13px] text-[color:var(--text-secondary)] mt-1 max-w-[760px]">
          The project's document tree, classified against the ontology grammar —
          area, series, name, date — nesting composes.
        </div>
      </div>

      <Card raised>
        <SectionLabel>Folders</SectionLabel>
        {data.folders.length === 0 ? (
          <Empty>No indexed documents yet.</Empty>
        ) : (
          <div>
            {data.folders.map((f) => (
              <FolderRow key={f.path} node={f} depth={0} />
            ))}
          </div>
        )}
      </Card>

      {(data.date_format_breaks.length > 0 || data.loose_in_root.length > 0) && (
        <Card>
          <SectionLabel>Grammar notes</SectionLabel>
          {data.date_format_breaks.map((p) => (
            <div key={p} className="text-xs text-[color:var(--accent-amber-fg)] py-0.5">
              {p} — breaks the one-format date rule
            </div>
          ))}
          {data.loose_in_root.map((p) => (
            <div key={p} className="text-xs text-[color:var(--accent-amber-fg)] py-0.5">
              {p} — loose in the root
            </div>
          ))}
        </Card>
      )}
    </Page>
  );
}
