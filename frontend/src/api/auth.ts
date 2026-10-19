import { apiFetch } from "./client";
import type { AuthResponse, LoginResponse } from "./types";

export function getSession() {
  return apiFetch<AuthResponse>("/api/auth/me");
}

export function login(email: string, password: string) {
  return apiFetch<LoginResponse>("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

// Шаг «код допуска»: почта, пароль и код одним запросом — допуск и сессия.
export function admit(email: string, password: string, code: string) {
  return apiFetch<AuthResponse>("/api/auth/admit", {
    method: "POST",
    body: JSON.stringify({ email, password, code }),
  });
}

export function register(email: string, password: string) {
  return apiFetch<LoginResponse>("/api/auth/register", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export function logout() {
  return apiFetch<{ ok: boolean }>("/api/auth/logout", { method: "POST" });
}
