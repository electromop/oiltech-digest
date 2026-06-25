import { apiFetch } from "./client";
import type { User } from "./types";

export function listUsers() {
  return apiFetch<{ users: User[] }>("/api/users").then((r) => r.users);
}

export function createUser(email: string, password: string, role: "admin" | "user") {
  return apiFetch<{ ok: boolean; user: User }>("/api/users", {
    method: "POST",
    body: JSON.stringify({ email, password, role }),
  });
}

export function updateUser(userId: number, patch: { role?: "admin" | "user"; password?: string }) {
  return apiFetch<{ ok: boolean; user: User }>(`/api/users/${userId}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function deleteUser(userId: number) {
  return apiFetch<{ ok: boolean }>(`/api/users/${userId}`, {
    method: "DELETE",
  });
}
