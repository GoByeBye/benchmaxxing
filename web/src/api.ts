import type { components } from './api-types';

export type Run = components['schemas']['RunRecord'];
export type RunSpec = components['schemas']['RunSpec'];
export type RunDetail = components['schemas']['RunDetail'];
export type RunEvent = components['schemas']['RunEvent'];
export type Artifact = components['schemas']['ArtifactRecord'];
export type Doctor = components['schemas']['DoctorReport'];
export type Task = components['schemas']['TaskSpec'];
export type Session = components['schemas']['SessionInfo'];

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api/v1${path}`, { ...options, credentials: 'same-origin' });
  if (!response.ok) {
    let message = `Request failed (${response.status}).`;
    try {
      const body = await response.json();
      if (typeof body.detail === 'string') message = body.detail;
      else if (Array.isArray(body.detail)) message = body.detail.map((item: { msg?: string }) => item.msg ?? 'Invalid configuration').join('; ');
    } catch { /* The HTTP status remains useful if the server returned no JSON. */ }
    throw new ApiError(message, response.status);
  }
  return response.json() as Promise<T>;
}

export function mutate<T>(path: string, token: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Benchmaxxing-Token': token },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export const isActive = (status: string) => status === 'queued' || status === 'running';
export const statusLabel = (status: string) => status.replaceAll('_', ' ');
export const participantLabel = (run: Run) => run.spec.participant === 'openai' ? run.spec.inference?.model ?? 'Model endpoint' : `Scripted · ${run.spec.scripted_variant ?? 'good'}`;
export const formatTime = (value: string) => new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
export const formatDate = (value: string) => new Date(value).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
