/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_BRAND_LOGO_PATH?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
