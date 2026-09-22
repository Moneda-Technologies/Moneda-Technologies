import { customerCompanyApi, priceListApi } from "../api";
import { openModal } from "../components/modal";
import { pageScaffold } from "../components/page";
import { refreshIcons } from "../components/icons";
import { toast } from "../components/toast";
import { appStore } from "../state/store";
import type { Customer, PriceListDefinition } from "../types/domain";
import { escapeHtml } from "../utils/dom";
import { openCustomerEditor } from "./customers";

/* Legacy fallback definitions intentionally retained only as a migration
   reference; the UI never uses them. const fallbackPriceLists: PriceListDefinition[] = [
  {
    _id: "blankets", display_name: "Blankets", description: "Blanket commercial price list", category: "blankets",
    classification: "GLOBAL", client_type: null,
    document_url: "https://workdrive.zohoexternal.in/embed/dgl7a5a1296292fb94375948159d0621cc4af?toolbar=false&appearance=light&themecolor=green",
    pdf_path: "data/price_lists/Blanket-Price-List.pdf", pdf_filename: "Moneda-Blanket-Price-List.pdf",
    active: true, sort_order: 10, metadata: { title: "Blankets Price List" },
  },
  {
    _id: "underpacking-dealer", display_name: "Underpacking — Dealer", description: "Dealer underpacking price list", category: "mpacks",
    classification: "DEALER", client_type: "DEALER", document_url: "https://workdrive.zohoexternal.in/embed/dgl7a89ce96e61ed145e3a7c154116977cde4?toolbar=false&appearance=light&themecolor=green",
    pdf_path: "data/price_lists/Dealer.pdf", pdf_filename: "Moneda-Dealer-Price-List.pdf", active: true, sort_order: 20,
    metadata: { title: "Underpacking Dealer Price List" },
  },
  {
    _id: "underpacking-distributor", display_name: "Underpacking — Distributor", description: "Distributor underpacking price list", category: "mpacks",
    classification: "DISTRIBUTOR", client_type: "WHOLESALER", document_url: "https://workdrive.zohoexternal.in/embed/dgl7a43f6bd264f9e4a5f80d8b9d2242d7d25?toolbar=false&appearance=light&themecolor=green",
    pdf_path: "data/price_lists/Distributor.pdf", pdf_filename: "Moneda-Distributor-Price-List.pdf", active: true, sort_order: 30,
    metadata: { title: "Underpacking Distributor Price List" },
  },
]; */

function displayTitle(list: PriceListDefinition): string {
  return String(list.metadata?.title ?? `${list.display_name} Price List`);
}

function metadataValue(list: PriceListDefinition, key: string): string {
  const value = list.metadata?.[key];
  return value === undefined || value === null ? "" : String(value);
}

function priceListVersion(list: PriceListDefinition): string {
  return String(list.version ?? (metadataValue(list, "version") || "Current official reference"));
}

function priceListEffectiveDate(list: PriceListDefinition): string {
  const from = list.valid_from ? new Date(list.valid_from).toLocaleDateString() : "See official document";
  const until = list.valid_until ? new Date(list.valid_until).toLocaleDateString() : "";
  return until ? `${from} – ${until}` : from;
}

function priceListType(list: PriceListDefinition): string {
  const type = String(list.classification ?? list.client_type ?? "GLOBAL").toUpperCase();
  return type === "WHOLESALER" ? "Distributor" : type.charAt(0) + type.slice(1).toLowerCase();
}

function priceListOrientation(list: PriceListDefinition): "portrait" | "landscape" | "mixed" {
  const value = String(list.metadata?.page_orientation ?? "").toLowerCase();
  return value === "portrait" || value === "mixed" ? value : "landscape";
}

function externalViewerUrl(list: PriceListDefinition): string {
  const source = String(list.document_url ?? "").trim();
  if (!source) return "";
  try {
    const url = new URL(source);
    // WorkDrive hides its dark document controls when toolbar=false. Keep the
    // official remote document, but present it consistently with local PDFs.
    url.searchParams.set("toolbar", "true");
    return url.toString();
  } catch {
    return source.replace(/([?&])toolbar=false(?=&|$)/i, "$1toolbar=true");
  }
}

function customerEmail(customer: Customer): string {
  const email = String(customer.email ?? "").trim();
  return email === "-" ? "" : email;
}

function customerEmails(customer: Customer): string[] {
  const candidate = customer as Customer & { emails?: unknown; email_contacts?: unknown; contacts?: unknown };
  const values: unknown[] = [customerEmail(customer)];
  for (const field of [candidate.emails, candidate.email_contacts, candidate.contacts]) {
    if (Array.isArray(field)) values.push(...field);
  }
  return [...new Set(values.flatMap((value) => {
    if (typeof value === "string") return value.split(",").map((item) => item.trim());
    if (value && typeof value === "object") {
      const email = (value as { email?: unknown }).email;
      return typeof email === "string" ? [email.trim()] : [];
    }
    return [];
  }).filter((email) => email && email !== "-"))];
}

interface RecipientEditor {
  setValues(values: string[]): void;
  values(): string[];
}

function setupRecipientEditor(root: HTMLElement, initial: string[] = []): RecipientEditor {
  const input = root.querySelector<HTMLInputElement>("input")!;
  const chips = root.querySelector<HTMLElement>("[data-recipient-chips]")!;
  let recipients: string[] = [];
  const emailPattern = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  const render = () => {
    chips.innerHTML = recipients.map((email) => `<span class="recipient-chip">${escapeHtml(email)}<button type="button" data-remove-recipient="${escapeHtml(email)}" aria-label="Remove ${escapeHtml(email)}"><i data-lucide="x"></i></button></span>`).join("");
    chips.querySelectorAll<HTMLButtonElement>("[data-remove-recipient]").forEach((button) => button.addEventListener("click", () => {
      recipients = recipients.filter((email) => email !== button.dataset.removeRecipient);
      render();
    }));
    refreshIcons(chips);
  };
  const add = (raw: string, strict = false) => {
    const values = raw.split(/[;,]/).map((value) => value.trim()).filter(Boolean);
    const invalid = values.find((value) => !emailPattern.test(value));
    if (invalid && strict) throw new Error(`Enter a valid email address: ${invalid}`);
    recipients = [...new Set([...recipients, ...values.filter((value) => emailPattern.test(value)).map((value) => value.toLowerCase())])];
    input.value = invalid && !strict ? invalid : "";
    render();
  };
  input.addEventListener("keydown", (event) => {
    if (!["Enter", ",", ";", "Tab"].includes(event.key) || !input.value.trim()) return;
    event.preventDefault(); add(input.value);
  });
  input.addEventListener("blur", () => { if (input.value.trim()) add(input.value); });
  recipients = [...new Set(initial.map((value) => value.trim().toLowerCase()).filter((value) => emailPattern.test(value)))];
  render();
  return {
    setValues(values) { recipients = [...new Set(values.map((value) => value.trim().toLowerCase()).filter((value) => emailPattern.test(value)))]; input.value = ""; render(); },
    values() { if (input.value.trim()) add(input.value, true); return [...recipients]; },
  };
}

async function openSendModal(list: PriceListDefinition): Promise<void> {
  const result = await customerCompanyApi.list();
  const customers = [...(result.items ?? [])];
  const content = document.createElement("div");
  const customerOption = (customer: Customer) => `<option value="${escapeHtml(customer._id)}">${escapeHtml(customer.name)}${customerEmail(customer) ? ` — ${escapeHtml(customerEmail(customer))}` : ""}</option>`;
  const options = customers.map(customerOption).join("");
  const defaultSubject = `Moneda Technologies — ${list.display_name}`;
  content.innerHTML = `<form class="stack-form price-list-send-form"><p class="form-hint">Send <strong>${escapeHtml(list.display_name)}</strong> using the selected customer’s authorized contact and optional additional recipients.</p><label for="price-list-send-customer">Customer<select id="price-list-send-customer" name="customer_id" required><option value="">Select customer</option>${options}<option value="__new_customer__">+ New Customer</option></select></label><label>To *<div class="recipient-editor" data-recipient-editor="to"><div class="recipient-chips" data-recipient-chips></div><input id="price-list-send-to" type="email" inputmode="email" autocomplete="email" placeholder="Type an email and press Enter"></div><span class="form-hint">The selected customer’s authorized email is added automatically.</span></label><label>CC (optional)<div class="recipient-editor" data-recipient-editor="cc"><div class="recipient-chips" data-recipient-chips></div><input id="price-list-send-cc" type="email" inputmode="email" placeholder="Type an email and press Enter"></div></label><label>BCC (optional)<div class="recipient-editor" data-recipient-editor="bcc"><div class="recipient-chips" data-recipient-chips></div><input id="price-list-send-bcc" type="email" inputmode="email" placeholder="Type an email and press Enter"></div></label><label for="price-list-send-subject">Subject<input id="price-list-send-subject" name="subject" type="text" required value="${escapeHtml(defaultSubject)}"></label><label for="price-list-send-message">Message<textarea id="price-list-send-message" name="message" rows="4">Dear customer,\n\nPlease find the requested Moneda Technologies price list attached.\n\nRegards,\nMoneda Technologies</textarea></label><small class="field-error" data-send-error></small><div class="modal-actions"><button type="button" class="button button-quiet" data-cancel>Cancel</button><button type="submit" class="button button-primary"><i data-lucide="send"></i>Send Price List</button></div></form>`;
  const dialog = openModal("Send price list", content, "wide");
  const form = content.querySelector<HTMLFormElement>("form")!;
  const customer = content.querySelector<HTMLSelectElement>("#price-list-send-customer")!;
  const newCustomerOption = customer.querySelector<HTMLOptionElement>('option[value="__new_customer__"]');
  newCustomerOption?.remove();
  const addCustomerButton = document.createElement("button");
  addCustomerButton.type = "button";
  addCustomerButton.className = "button button-quiet price-list-add-customer";
  addCustomerButton.innerHTML = '<i data-lucide="plus"></i>Add New Customer';
  customer.closest("label")?.append(addCustomerButton);
  const editors = {
    to: setupRecipientEditor(content.querySelector<HTMLElement>('[data-recipient-editor="to"]')!),
    cc: setupRecipientEditor(content.querySelector<HTMLElement>('[data-recipient-editor="cc"]')!),
    bcc: setupRecipientEditor(content.querySelector<HTMLElement>('[data-recipient-editor="bcc"]')!),
  };
  let previousCustomerId = "";
  addCustomerButton.addEventListener("click", async () => {
    await openCustomerEditor(undefined, () => undefined, {
      title: "Add Customer for Price List",
      suppressSuccessDialog: true,
      onCreated: (created) => {
        customers.push(created);
        const option = document.createElement("option");
        option.value = created._id;
        option.textContent = `${created.name}${customerEmail(created) ? ` — ${customerEmail(created)}` : ""}`;
        customer.append(option);
        customer.value = created._id;
        previousCustomerId = created._id;
        editors.to.setValues(customerEmails(created));
      },
    });
  });
  customer.addEventListener("change", async () => {
    if (customer.value === "__new_customer__") {
      customer.value = previousCustomerId;
      await openCustomerEditor(undefined, () => undefined, {
        title: "Add Customer for Price List",
        suppressSuccessDialog: true,
        onCreated: (created) => {
          customers.push(created);
          const newCustomerOption = document.createElement("option");
          newCustomerOption.value = created._id;
          newCustomerOption.textContent = `${created.name}${customerEmail(created) ? ` — ${customerEmail(created)}` : ""}`;
          customer.insertBefore(newCustomerOption, customer.querySelector('option[value="__new_customer__"]'));
          customer.value = created._id;
          previousCustomerId = created._id;
          editors.to.setValues(customerEmails(created));
        },
      });
      return;
    }
    previousCustomerId = customer.value;
    const selected = customers.find((item) => item._id === customer.value);
    editors.to.setValues(selected ? customerEmails(selected) : []);
  });
  content.querySelector("[data-cancel]")?.addEventListener("click", () => dialog.close());
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector<HTMLButtonElement>("[type=submit]")!;
    const data = new FormData(form);
    button.disabled = true;
    try {
      const to = editors.to.values();
      const cc = editors.cc.values();
      const bcc = editors.bcc.values();
      if (!customer.value) throw new Error("Select a customer");
      if (!to.length) throw new Error("Add at least one valid To recipient");
      await priceListApi.send(list._id, {
        customer_id: customer.value, to, cc, bcc,
        subject: data.get("subject"), message: data.get("message"),
      });
      dialog.close();
      toast("Price list email sent with the official PDF attachment");
    } catch (error) {
      const message = error instanceof Error ? error.message : "Price list email could not be sent";
      const errorNode = content.querySelector<HTMLElement>("[data-send-error]");
      if (errorNode) errorNode.textContent = message;
      toast(message, "error");
      button.disabled = false;
    }
  });
  refreshIcons(content);
}

export async function accountPriceListsPage(): Promise<HTMLElement> {
  const page = pageScaffold("Account", "Price Lists", "Official Moneda price lists and commercial reference documents.", '<a class="button button-secondary" href="/profile" data-route="/profile"><i data-lucide="arrow-left"></i>Back to Account</a>');
  page.classList.add("account-price-lists-page");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = '<div class="notice"><i data-lucide="loader-circle"></i><div>Loading price lists…</div></div>';
  let lists: PriceListDefinition[] = [];
  let loadError = "";
  try { lists = (await priceListApi.list()).items.filter((item) => item.active !== false); }
  catch (error) { loadError = error instanceof Error ? error.message : "The price-list service is unavailable."; }
  let selectedId = lists[0]?._id ?? "";
  let query = "";
  let category = "";
  const categories = [...new Set(lists.map((list) => String(list.category ?? "")).filter(Boolean))].sort();
  const render = () => {
    const filtered = lists.filter((list) => (!category || list.category === category) && (!query || `${list.display_name} ${list.description ?? ""}`.toLocaleLowerCase().includes(query.toLocaleLowerCase())));
    const selected = lists.find((list) => list._id === selectedId) ?? filtered[0] ?? lists[0];
    if (selected) selectedId = selected._id;
    const viewerUrl = selected?.pdf_path
      ? `${priceListApi.documentUrl(selected._id)}#view=FitH&toolbar=1&navpanes=0`
      : selected ? externalViewerUrl(selected) : "";
    const documentActions = selected?.pdf_path
      ? `<a class="button button-secondary" href="${escapeHtml(priceListApi.documentUrl(selected._id))}" target="_blank" rel="noopener" data-price-list-open><i data-lucide="external-link"></i>Open PDF</a><a class="button button-secondary" href="${escapeHtml(priceListApi.documentUrl(selected._id, true))}" download="${escapeHtml(selected.pdf_filename ?? "price-list.pdf")}" data-price-list-download><i data-lucide="download"></i>Download PDF</a>`
      : '<span class="price-list-asset-note"><i data-lucide="info"></i>Viewer available · PDF delivery asset not configured</span>';
    body.innerHTML = loadError ? `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Price lists unavailable</strong><p>${escapeHtml(loadError)}</p></div></div>` : `<section class="panel account-price-list-browser"><div class="account-price-list-browser-head"><div><span class="eyebrow">Commercial references</span><h2>Choose a price list</h2><p>Select an official document to view, download, or send to a customer.</p></div></div><div class="account-price-list-toolbar"><label for="account-price-list-search">Search price lists<input id="account-price-list-search" name="price_list_search" type="search" placeholder="Search price lists" value="${escapeHtml(query)}"></label><label for="account-price-list-category">Category<select id="account-price-list-category" name="price_list_category"><option value="">All categories</option>${categories.map((item) => `<option value="${escapeHtml(item)}" ${item === category ? "selected" : ""}>${escapeHtml(item === "mpacks" ? "Underpacking" : item)}</option>`).join("")}</select></label></div><div class="account-price-list-card-grid" role="list">${filtered.length ? filtered.map((list) => `<article role="listitem" class="account-price-list-card${list._id === selected?._id ? " is-active" : ""}" data-price-list-card="${escapeHtml(list._id)}"><div class="account-price-list-card__head"><strong>${escapeHtml(list.display_name)}</strong><span class="status-badge">${escapeHtml(priceListType(list))}</span></div><p>${escapeHtml(list.description ?? "Official Moneda reference")}</p><dl class="account-price-list-card__meta"><div><dt>Category</dt><dd>${escapeHtml(list.category === "mpacks" ? "Underpacking" : list.category ?? "All")}</dd></div><div><dt>Customer type</dt><dd>${escapeHtml(priceListType(list))}</dd></div><div><dt>Version</dt><dd>${escapeHtml(priceListVersion(list))}</dd></div><div><dt>Effective</dt><dd>${escapeHtml(priceListEffectiveDate(list))}</dd></div><div><dt>Status</dt><dd>${list.active === false ? "Inactive" : "Active"}</dd></div><div><dt>Document</dt><dd>${list.document_url ? "Available" : "Unavailable"}</dd></div></dl><div class="account-price-list-card__actions"><button type="button" class="button button-secondary" data-price-list-id="${escapeHtml(list._id)}"><i data-lucide="eye"></i>View</button>${appStore.can("price_list.send") && list.pdf_path ? `<button type="button" class="button button-primary" data-card-send="${escapeHtml(list._id)}"><i data-lucide="send"></i>Send</button>` : appStore.can("price_list.send") ? '<button type="button" class="button button-primary" disabled title="Official PDF attachment unavailable"><i data-lucide="send"></i>Send</button>' : ""}${list.pdf_path ? `<a class="button button-quiet" href="${escapeHtml(priceListApi.documentUrl(list._id, true))}" download="${escapeHtml(list.pdf_filename ?? "price-list.pdf")}"><i data-lucide="download"></i>Download</a>` : `<button type="button" class="button button-quiet" disabled title="Official PDF asset is unavailable"><i data-lucide="download"></i>Download</button>`}</div></article>`).join("") : '<div class="empty-state account-price-list-empty"><i data-lucide="file-search"></i><strong>No matching price lists</strong><span>Change the search or category filter.</span></div>'}</div></section>${selected ? `<section class="panel account-price-list-viewer" aria-labelledby="account-price-list-document-heading"><div class="account-price-list-viewer__header"><div><span class="eyebrow">${escapeHtml(selected.display_name)}</span><h2 id="account-price-list-document-heading">${escapeHtml(displayTitle(selected))}</h2><p class="form-hint">Official Moneda reference · ${selected.active === false ? "Inactive" : "Active"}</p></div><div class="account-price-list-viewer-actions">${documentActions}${appStore.can("price_list.send") && selected.pdf_path ? '<button type="button" class="button button-primary" data-send-price-list><i data-lucide="send"></i>Send to customer</button>' : ""}</div></div><div class="account-price-list-embed is-${priceListOrientation(selected)}" id="account-price-list-document" role="document" aria-label="${escapeHtml(displayTitle(selected))}"><iframe src="${escapeHtml(viewerUrl)}" title="${escapeHtml(displayTitle(selected))}" loading="lazy" scrolling="yes" frameborder="0" allowfullscreen="true" tabindex="-1"></iframe></div></section>` : ""}`;
    body.querySelector<HTMLInputElement>("#account-price-list-search")?.addEventListener("input", (event) => { query = (event.target as HTMLInputElement).value; render(); });
    body.querySelector<HTMLSelectElement>("#account-price-list-category")?.addEventListener("change", (event) => { category = (event.target as HTMLSelectElement).value; render(); });
    body.querySelectorAll<HTMLElement>("[data-price-list-id]").forEach((item) => item.addEventListener("click", () => { selectedId = item.dataset.priceListId ?? selectedId; render(); }));
    body.querySelectorAll<HTMLButtonElement>("[data-card-send]").forEach((button) => button.addEventListener("click", () => {
      const list = lists.find((item) => item._id === button.dataset.cardSend);
      if (list) openSendModal(list).catch((error) => toast(error instanceof Error ? error.message : "Customers could not be loaded", "error"));
    }));
    body.querySelector("[data-send-price-list]")?.addEventListener("click", () => { if (selected) openSendModal(selected).catch((error) => toast(error instanceof Error ? error.message : "Customers could not be loaded", "error")); });
    refreshIcons(body);
  };
  render();
  return page;
}
