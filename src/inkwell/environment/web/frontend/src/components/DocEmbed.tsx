export function DocLink({ docUrl }: { docUrl: string }) {
  if (!docUrl) {
    return (
      <span className="doc-pending">
        <span className="doc-link-icon">&#9998;</span>
        Doc will appear once the pipeline creates one
      </span>
    );
  }

  return (
    <a href={docUrl} target="_blank" rel="noopener noreferrer" className="doc-link">
      <span className="doc-link-icon">&#9998;</span>
      Open Google Doc
    </a>
  );
}
