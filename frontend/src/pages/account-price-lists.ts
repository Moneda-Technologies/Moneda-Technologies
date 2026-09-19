import { pageScaffold } from "../components/page";
import { escapeHtml } from "../utils/dom";

type PriceListDocument = {
  id: "blankets" | "dealer" | "distributor";
  label: string;
  description: string;
  src: string;
  title: string;
};

const PRICE_LIST_DOCUMENTS: readonly PriceListDocument[] = [
  {
    id: "blankets",
    label: "Blankets",
    description: "Blanket commercial price list",
    src: "https://workdrive.zohoexternal.in/embed/dgl7a5a1296292fb94375948159d0621cc4af?toolbar=false&appearance=light&themecolor=green",
    title: "Blankets Price List",
  },
  {
    id: "dealer",
    label: "Underpacking — Dealer",
    description: "Dealer underpacking price list",
    src: "https://workdrive.zohoexternal.in/embed/dgl7a89ce96e61ed145e3a7c154116977cde4?toolbar=false&appearance=light&themecolor=green",
    title: "Underpacking Dealer Price List",
  },
  {
    id: "distributor",
    label: "Underpacking — Distributor",
    description: "Distributor underpacking price list",
    src: "https://workdrive.zohoexternal.in/embed/dgl7a43f6bd264f9e4a5f80d8b9d2242d7d25?toolbar=false&appearance=light&themecolor=green",
    title: "Underpacking Distributor Price List",
  },
];

export async function accountPriceListsPage(): Promise<HTMLElement> {
  const page = pageScaffold(
    "Account",
    "Price Lists",
    "Official Moneda price lists and commercial reference documents.",
    '<a class="button button-secondary" href="/profile" data-route="/profile"><i data-lucide="arrow-left"></i>Back to Account</a>',
  );
  page.classList.add("account-price-lists-page");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  let selectedId: PriceListDocument["id"] = "blankets";

  const render = () => {
    const selected = PRICE_LIST_DOCUMENTS.find((document) => document.id === selectedId) ?? PRICE_LIST_DOCUMENTS[0];
    body.innerHTML = `
      <section class="panel account-price-list-selector" aria-label="Price list selection">
        <div class="account-price-list-selector__intro">
          <span class="eyebrow">Commercial references</span>
          <h2>Choose a price list</h2>
          <p>Select the official document you want to view.</p>
        </div>
        <div class="account-price-list-tabs" role="tablist" aria-label="Price lists">
          ${PRICE_LIST_DOCUMENTS.map((document) => `
            <button type="button" class="account-price-list-tab${document.id === selected.id ? " is-active" : ""}" role="tab" aria-selected="${document.id === selected.id}" aria-controls="account-price-list-document" data-price-list-id="${document.id}">
              <strong>${escapeHtml(document.label)}</strong>
              <span>${escapeHtml(document.description)}</span>
            </button>
          `).join("")}
        </div>
      </section>
      <section class="panel account-price-list-viewer" aria-labelledby="account-price-list-document-heading">
        <div class="account-price-list-viewer__header">
          <div>
            <span class="eyebrow">${escapeHtml(selected.label)}</span>
            <h2 id="account-price-list-document-heading">${escapeHtml(selected.title)}</h2>
          </div>
          <span class="form-hint">Official Moneda reference</span>
        </div>
        <div class="account-price-list-embed" id="account-price-list-document" role="tabpanel" aria-label="${escapeHtml(selected.title)}">
          <iframe src="${escapeHtml(selected.src)}" title="${escapeHtml(selected.title)}" loading="lazy" scrolling="no" frameborder="0" allowfullscreen="true"></iframe>
        </div>
      </section>`;

    body.querySelectorAll<HTMLButtonElement>("[data-price-list-id]").forEach((button) => {
      button.addEventListener("click", () => {
        const nextId = button.dataset.priceListId as PriceListDocument["id"] | undefined;
        if (!nextId || nextId === selectedId) return;
        selectedId = nextId;
        render();
      });
    });
  };

  render();
  return page;
}
