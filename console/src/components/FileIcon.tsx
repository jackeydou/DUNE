import { File, FileCode, FileCog, FileJson, FileText } from "lucide-react"

/** A case file's icon, by its extension. */
export function FileIcon({ path, className }: { path: string; className?: string }) {
  if (/\.ya?ml$/.test(path)) return <FileCog className={className} />
  if (path.endsWith(".json")) return <FileJson className={className} />
  if (path.endsWith(".py")) return <FileCode className={className} />
  if (path.endsWith(".md") || path.endsWith(".txt")) return <FileText className={className} />
  return <File className={className} />
}
