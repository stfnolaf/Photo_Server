import { Star } from "lucide-react";

export function Rating({
  value,
  disabled = false,
  onChange,
  compact = false,
}: {
  value: number;
  disabled?: boolean;
  onChange?: (value: number) => void;
  compact?: boolean;
}) {
  if (!onChange) {
    return value ? (
      <span className="rating-display" aria-label={`${value} stars`}>
        <Star size={compact ? 11 : 13} fill="currentColor" /> {value}
      </span>
    ) : null;
  }
  return (
    <div className={`rating ${compact ? "rating--compact" : ""}`} aria-label="Rating">
      {[1, 2, 3, 4, 5].map((rating) => (
        <button
          key={rating}
          type="button"
          className={rating <= value ? "is-filled" : ""}
          aria-label={`Rate ${rating} star${rating === 1 ? "" : "s"}`}
          aria-pressed={rating === value}
          disabled={disabled}
          onClick={() => onChange(rating === value ? 0 : rating)}
        >
          <Star size={compact ? 15 : 19} fill={rating <= value ? "currentColor" : "none"} />
        </button>
      ))}
    </div>
  );
}
