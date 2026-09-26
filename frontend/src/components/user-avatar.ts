import { escapeHtml } from "../utils/dom";
import { apiEndpoint } from "../api/client";

export interface AvatarUser {
  _id?: string;
  name?: string;
  email?: string;
  profile_photo_url?: string | null;
}

export function userInitials(user: AvatarUser | null | undefined): string {
  const name = String(user?.name || user?.email || "User").trim();
  return name.split(/\s+/).map((part) => part[0]).join("").slice(0, 2).toUpperCase() || "U";
}

export function userAvatar(user: AvatarUser | null | undefined, size = "medium", withName = false): string {
  const name = String(user?.name || user?.email || "User");
  const fallback = escapeHtml(userInitials(user));
  const photo = String(user?.profile_photo_url || "");
  const image = photo ? `<img src="${escapeHtml(apiEndpoint(photo))}" alt="${escapeHtml(name)}" loading="lazy" data-avatar-image onerror="this.style.display='none';this.nextElementSibling?.removeAttribute('hidden')"><span class="user-avatar-fallback" hidden>${fallback}</span>` : fallback;
  const avatar = `<span class="user-avatar user-avatar-${escapeHtml(size)}" data-user-id="${escapeHtml(String(user?._id || ""))}" aria-label="${escapeHtml(name)}">${image}</span>`;
  return withName ? `<span class="user-avatar-with-name">${avatar}<span>${escapeHtml(name)}</span></span>` : avatar;
}
