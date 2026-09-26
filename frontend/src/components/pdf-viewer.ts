import { apiEndpoint } from "../api/client";
import { refreshIcons } from "./icons";
import { openModal } from "./modal";

export class PdfViewerError extends Error {
  constructor(message: string, public readonly status?: number) {
    super(message);
  }
}

export interface PdfObjectUrl {
  url: string;
  revoke: () => void;
}

export interface PdfViewerOptions {
  title: string;
  path: string;
  filename?: string;
  subtitle?: string;
}

function errorMessage(status: number): string {
  if (status === 401) return "Your session has expired. Sign in again to view this PDF.";
  if (status === 403) return "You do not have permission to view this PDF.";
  if (status === 404) return "This PDF is no longer available.";
  if (status >= 500) return "The PDF could not be loaded right now. Please try again.";
  return `The PDF could not be loaded (HTTP ${status}).`;
}

export async function fetchPdfObjectUrl(path: string): Promise<PdfObjectUrl> {
  const response = await fetch(apiEndpoint(path), {
    credentials: "include",
    headers: { Accept: "application/pdf" },
  });
  if (!response.ok) throw new PdfViewerError(errorMessage(response.status), response.status);
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.toLowerCase().includes("application/pdf")) {
    throw new PdfViewerError("The server returned an unexpected document format.", response.status);
  }
  const blob = await response.blob();
  if (!blob.size) throw new PdfViewerError("The PDF response was empty.", response.status);
  const url = URL.createObjectURL(blob);
  let revoked = false;
  return {
    url,
    revoke: () => {
      if (revoked) return;
      revoked = true;
      URL.revokeObjectURL(url);
    },
  };
}

export function openPdfViewer(options: PdfViewerOptions): void {
  const content = document.createElement("div");
  content.className = "pdf-viewer";
  content.innerHTML = `<p class="pdf-viewer-subtitle">${options.subtitle ?? "Loading immutable PDF…"}</p><div class="pdf-viewer-state" data-pdf-state><span class="signature-upload-spinner" aria-hidden="true"></span><span>Loading PDF…</span></div><iframe class="pdf-viewer-frame" title="${options.title}" hidden></iframe><div class="modal-actions"><a class="button button-secondary" data-pdf-download hidden><i data-lucide="download"></i>Download</a><button type="button" class="button button-quiet" data-pdf-close>Close</button></div>`;
  const dialog = openModal(options.title, content, "wide", { autoFocus: false });
  const state = content.querySelector<HTMLElement>("[data-pdf-state]")!;
  const frame = content.querySelector<HTMLIFrameElement>(".pdf-viewer-frame")!;
  const download = content.querySelector<HTMLAnchorElement>("[data-pdf-download]")!;
  let documentUrl: PdfObjectUrl | null = null;

  content.querySelector<HTMLButtonElement>("[data-pdf-close]")?.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => documentUrl?.revoke(), { once: true });
  void fetchPdfObjectUrl(options.path).then((loaded) => {
    documentUrl = loaded;
    frame.src = loaded.url;
    frame.hidden = false;
    download.href = loaded.url;
    download.download = options.filename ?? "moneda-document.pdf";
    download.hidden = false;
    state.hidden = true;
    refreshIcons(content);
  }).catch((error: unknown) => {
    state.classList.add("is-error");
    state.innerHTML = `<i data-lucide="circle-alert"></i><span>${error instanceof Error ? error.message : "The PDF could not be loaded."}</span>`;
    refreshIcons(state);
  });
}
