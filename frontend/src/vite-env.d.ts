/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_BRAND_LOGO_PATH?: string;
  readonly VITE_API_ORIGIN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
