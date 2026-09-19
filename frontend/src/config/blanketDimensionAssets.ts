/**
 * Replaceable artwork configuration for the blanket dimension fields.
 *
 * Keep the artwork URL/path separate from the calculator markup so the supplied
 * SVG artwork can be replaced later without changing dimension state or pricing
 * code.
 */
interface BlanketDimensionAsset {
  src: string;
  alt: string;
}

export const BLANKET_DIMENSION_ASSETS: Record<"across" | "around", BlanketDimensionAsset> = {
  across: {
    src: "/diagrams/blanket-across.png",
    alt: "Across (width): horizontal blanket roll with a horizontal double-headed measurement arrow",
  },
  around: {
    src: "/diagrams/Around.png",
    alt: "Around (length): blanket roll with a curved circumference measurement arrow",
  },
} as const;
