export type MediaType = 'movie' | 'tv';
export type LibraryType = 'series' | 'movies';
export interface Envelope {
  status?: string;
  message?: string;
  detail?: string;
  error?: string;
}
export interface TmdbInfo {
  poster?: string;
  match_status?: string;
  diff?: number | null;
  declared_total?: number | null;
  tmdb_total?: number | null;
  seasons?: { season: number; tmdb: number }[];
}
export interface MediaCard {
  tmdb_id?: string;
  type?: MediaType;
  title?: string;
  name?: string;
  id?: string;
  year?: string | number;
  poster?: string;
  poster_tmdb?: string;
  has_image?: boolean;
  rating?: number;
  in_emby?: boolean | null;
  in_local?: boolean | null;
  in_share?: boolean | null;
  library_status?: string;
  eps?: { have?: number; total?: number; [key: string]: unknown } | null;
  tmdb_info?: TmdbInfo;
  have_eps?: number | null;
  total_episodes?: number;
  total_seasons?: number;
  local_eps?: number | null;
  share_eps?: number | null;
}
export interface LibraryItem extends MediaCard {
  _md?: boolean;
  path?: string;
  paths?: string[];
  seasons?: {
    season: number;
    count?: number;
    episodes?: number[];
    local_eps?: number;
    share_eps?: number;
  }[];
}
export interface ExploreResponse extends Envelope {
  cards?: MediaCard[];
  page?: number;
  total_pages?: number;
  library_error?: string;
  library_refreshing?: boolean;
}
export interface LibraryPage extends Envelope {
  rows?: MediaCard[];
  total?: number;
  next_offset?: number;
  has_more?: boolean;
  facts_ts?: number;
  facts_version?: string;
  stale?: boolean;
}
export interface Subscription {
  id: string;
  tmdb_id: string;
  name: string;
  poster?: string;
  enabled: boolean;
  added_at?: string;
  latest_ep?: string;
  have_eps?: number | null;
  tmdb_declared?: number | null;
  tmdb_total?: number | null;
  tmdb_status?: string;
  check_error?: string;
  facts_status?: string;
}
export interface SubscriptionsResponse extends Envelope {
  subscriptions: Subscription[];
  enabled: boolean;
  check_tmdb: boolean;
  calibration_interval_hours: number;
  ingest_enabled?: boolean;
}
export interface Runtime extends Envelope {
  version?: string;
  readiness?: { ready?: boolean; phase?: string };
  memory?: { rss_mib?: number };
  task?: { kind?: string; source?: string } | null;
  bot?: { running?: boolean; state?: string };
}
export interface Dashboard {
  localCount?: string;
  shareCount?: string;
  services?: Record<string, { ok?: boolean; host?: string; bot_running?: boolean }>;
  libraryHealth?: {
    stats?: Record<string, number>;
    episodes?: number;
    stale?: boolean;
    ts?: number;
  };
  ingest?: { movies?: number; series?: number; episodes?: number };
  subscriptions?: { total?: number; enabled?: number };
  lastScan?: { time?: string; total?: number; protected?: number } | null;
}
export interface Overview extends Envelope {
  dashboard?: Dashboard;
  ts?: number;
  stale?: boolean;
  refresh?: { running?: boolean; phase?: string; error?: string; warnings?: string[] };
  activity?: {
    recent?: (MediaCard & {
      media_type?: MediaType;
      episodes?: string;
      ts?: number;
      library?: string;
      category?: string;
      count?: number;
    })[];
    ingest_error?: boolean;
  };
}
export type JobState =
  | 'queued'
  | 'running'
  | 'success'
  | 'failed'
  | 'uncertain'
  | 'needs_review'
  | 'cancelled'
  | 'stopped';
export interface Job {
  id: string;
  kind: string;
  state: JobState;
  message?: string;
  created?: number;
  updated?: number;
  account_id?: string;
  upgrades?: unknown[];
  steps?: { step?: string; step_key?: string; state?: string; result?: unknown }[];
  progress?: {
    total?: number | null;
    processed?: number;
    phase?: string;
    current?: string;
    updated?: number;
  };
  activity?: { label?: string; active?: boolean; needs_attention?: boolean };
  result?: unknown;
}
export interface Diagnostic {
  name: string;
  category: string;
  label: string;
  reason: string;
  stage: string;
  suggestion: string;
  path?: string;
  rule?: string;
}
export interface DiagnosticPage {
  items: Diagnostic[];
  total: number;
  has_more: boolean;
  offset: number;
}
export interface CloudAccount {
  id: string;
  name?: string;
  uid?: string;
  health?: string;
  status?: string;
}
export interface CloudFile {
  id: string;
  name: string;
  is_dir: boolean;
  size?: number;
  cid?: string;
}
export interface FilePage extends Envelope {
  items: CloudFile[];
  total?: number;
  offset?: number;
  has_more?: boolean;
  breadcrumb?: { cid: string; name: string }[];
  path?: { cid: string; name: string }[];
}
