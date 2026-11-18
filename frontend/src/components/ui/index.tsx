import * as DialogP from "@radix-ui/react-dialog";
import * as SliderP from "@radix-ui/react-slider";
import * as SwitchP from "@radix-ui/react-switch";
import * as TooltipP from "@radix-ui/react-tooltip";
import { cva, type VariantProps } from "class-variance-authority";
import { Loader2, X } from "lucide-react";
import { forwardRef, type ButtonHTMLAttributes, type HTMLAttributes, type ReactNode } from "react";
import { cn } from "@/lib/format";

const button = cva(
  "focus-ring inline-flex items-center justify-center gap-1.5 whitespace-nowrap font-semibold transition-colors disabled:pointer-events-none disabled:opacity-40",
  {
    variants: {
      variant: {
        primary: "bg-violet-500 text-white hover:bg-violet-400",
        secondary: "border border-ink-600 text-ink-100 hover:border-ink-500 hover:bg-ink-800",
        ghost: "text-ink-300 hover:text-ink-100 hover:bg-ink-750",
        danger: "bg-live/15 text-live border border-live/30 hover:bg-live/25",
        volt: "bg-volt-500/15 text-volt-400 border border-volt-500/30 hover:bg-volt-500/25",
      },
      size: { xs: "h-6 px-2 text-[11px]", sm: "h-7 px-2.5 text-xs", md: "h-8 px-3 text-sm", lg: "h-10 px-4 text-sm" },
    },
    defaultVariants: { variant: "secondary", size: "sm" },
  },
);

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement>, VariantProps<typeof button> {
  loading?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(({ className, variant, size, loading, children, ...p }, ref) => (
  <button ref={ref} className={cn(button({ variant, size }), className)} {...p} disabled={p.disabled || loading}>
    {loading && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
    {children}
  </button>
));
Button.displayName = "Button";

export function Tip({ label, children, side = "top" }: { label: ReactNode; children: ReactNode; side?: "top" | "bottom" | "left" | "right" }) {
  return (
    <TooltipP.Root delayDuration={250}>
      <TooltipP.Trigger asChild>{children}</TooltipP.Trigger>
      <TooltipP.Portal>
        <TooltipP.Content side={side} sideOffset={6}
          className="z-50 border border-ink-600 bg-ink-850 px-2 py-1 text-[11px] text-ink-100 shadow animate-slideIn">
          {label}
        </TooltipP.Content>
      </TooltipP.Portal>
    </TooltipP.Root>
  );
}

export const IconBtn = forwardRef<HTMLButtonElement, ButtonHTMLAttributes<HTMLButtonElement> & { label: string; active?: boolean; tone?: "violet" | "volt" | "live" | "ok" }>(
  ({ label, active, tone = "violet", className, children, ...p }, ref) => {
    const activeTone = { violet: "bg-violet-500/20 text-violet-400 border-violet-500/50", volt: "bg-volt-500/20 text-volt-400 border-volt-500/50",
      live: "bg-live/20 text-live border-live/50", ok: "bg-ok/15 text-ok border-ok/40" }[tone];
    return (
      <Tip label={label}>
        <button ref={ref} aria-label={label} aria-pressed={active}
          className={cn("focus-ring inline-flex h-7 w-7 items-center justify-center gap-1 border border-transparent text-ink-300 transition-colors hover:bg-ink-800 hover:text-ink-100 disabled:opacity-35",
            active && activeTone, className)} {...p}>
          {children}
        </button>
      </Tip>
    );
  },
);
IconBtn.displayName = "IconBtn";

export const Badge = forwardRef<HTMLSpanElement, HTMLAttributes<HTMLSpanElement>>(({ className, ...props }, ref) => (
  <span ref={ref} className={cn("inline-flex items-center gap-1 border px-1.5 py-px font-mono text-[10px] font-semibold uppercase tracking-wide", className)} {...props} />
));
Badge.displayName = "Badge";

export function Switch({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label?: string }) {
  return (
    <SwitchP.Root checked={checked} onCheckedChange={onChange} aria-label={label}
      className="focus-ring relative h-5 w-9 shrink-0 border border-ink-600 bg-ink-750 transition-colors data-[state=checked]:border-violet-500 data-[state=checked]:bg-violet-500">
      <SwitchP.Thumb className="block h-3.5 w-3.5 translate-x-0.5 bg-ink-200 transition-transform data-[state=checked]:translate-x-[18px] data-[state=checked]:bg-white" />
    </SwitchP.Root>
  );
}

export function Slider({ value, onChange, min, max, step = 1, className, label }: {
  value: number[]; onChange: (v: number[]) => void; min: number; max: number; step?: number; className?: string; label?: string;
}) {
  return (
    <SliderP.Root value={value} onValueChange={onChange} min={min} max={max} step={step} aria-label={label}
      className={cn("relative flex h-5 w-full touch-none select-none items-center", className)}>
      <SliderP.Track className="relative h-1 grow overflow-hidden bg-ink-700">
        <SliderP.Range className="absolute h-full bg-violet-500" />
      </SliderP.Track>
      {value.map((_, i) => (
        <SliderP.Thumb key={i} className="focus-ring block h-4 w-2 border border-violet-600 bg-violet-500" aria-label={value.length === 2 ? (i === 0 ? "Trim start" : "Trim end") : label} />
      ))}
    </SliderP.Root>
  );
}

export function Dialog({ open, onOpenChange, title, description, children, wide }: {
  open: boolean; onOpenChange: (o: boolean) => void; title: string; description?: ReactNode; children: ReactNode; wide?: boolean;
}) {
  return (
    <DialogP.Root open={open} onOpenChange={onOpenChange}>
      <DialogP.Portal>
        <DialogP.Overlay className="fixed inset-0 z-40 bg-black/60 backdrop-blur-[2px]" />
        <DialogP.Content className={cn("panel fixed left-1/2 top-1/2 z-50 max-h-[85vh] w-[92vw] -translate-x-1/2 -translate-y-1/2 overflow-y-auto bg-ink-850 p-5",
          wide ? "max-w-3xl" : "max-w-md")}>
          <div className="mb-4 flex items-start justify-between gap-4">
            <div>
              <DialogP.Title className="font-display text-2xl font-bold">{title}</DialogP.Title>
              {description && <DialogP.Description className="mt-1 text-xs text-ink-300">{description}</DialogP.Description>}
            </div>
            <DialogP.Close className="focus-ring rounded p-1 text-ink-400 hover:text-ink-100" aria-label="Close"><X className="h-4 w-4" /></DialogP.Close>
          </div>
          {children}
        </DialogP.Content>
      </DialogP.Portal>
    </DialogP.Root>
  );
}

export function Confirm({ open, onOpenChange, title, body, confirmLabel, onConfirm, danger = true }: {
  open: boolean; onOpenChange: (o: boolean) => void; title: string; body: ReactNode; confirmLabel: string; onConfirm: () => void; danger?: boolean;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={title} description={<span className="text-sm text-ink-200">{body}</span>}>
      <div className="mt-5 flex justify-end gap-2">
        <Button variant="ghost" size="md" onClick={() => onOpenChange(false)}>Cancel</Button>
        <Button variant={danger ? "danger" : "primary"} size="md" autoFocus onClick={() => { onConfirm(); onOpenChange(false); }}>{confirmLabel}</Button>
      </div>
    </Dialog>
  );
}

export function Meter({ value, color = "violet", vertical, label, peak }: { value: number | null; color?: "violet" | "volt" | "ok" | "amber"; vertical?: boolean; label?: string; peak?: boolean }) {
  const v = Math.max(0, Math.min(1, value ?? 0));
  const tone = { violet: "bg-violet-500", volt: "bg-volt-500", ok: "bg-ok", amber: "bg-warn" }[color];
  return (
    <div className={cn("relative overflow-hidden bg-ink-700/70", vertical ? "h-full w-1.5" : "h-1.5 w-full", value == null && "stripes")} aria-label={label}
      role="meter" aria-valuenow={Math.round(v * 100)} aria-valuemin={0} aria-valuemax={100}>
      <div className={cn("absolute bottom-0 left-0 transition-all duration-300 ease-out", tone, vertical ? "w-full" : "h-full", peak && "brightness-125")}
        style={vertical ? { height: `${v * 100}%` } : { width: `${v * 100}%` }} />
    </div>
  );
}

export function PlatformIcon({ platform, className }: { platform: string; className?: string }) {
  if (platform === "twitch")
    return <svg viewBox="0 0 24 24" className={cn("h-3.5 w-3.5", className)} fill="#a970ff" aria-label="Twitch"><path d="M4.3 3 3 6.4V20h4.6v2.5h2.6L12.7 20h3.7l5-5V3H4.3zm15.4 11.1-2.9 2.9h-4.6l-2.5 2.5V17H5.9V4.7h13.8v9.4zM16.8 7.9v5h-1.7v-5h1.7zm-4.6 0v5h-1.7v-5h1.7z" /></svg>;
  if (platform === "youtube")
    return <svg viewBox="0 0 24 24" className={cn("h-3.5 w-3.5", className)} aria-label="YouTube"><path fill="#ff0033" d="M23 7.2a3 3 0 0 0-2.1-2.1C19 4.6 12 4.6 12 4.6s-7 0-8.9.5A3 3 0 0 0 1 7.2 31 31 0 0 0 .5 12a31 31 0 0 0 .5 4.8 3 3 0 0 0 2.1 2.1c1.9.5 8.9.5 8.9.5s7 0 8.9-.5a3 3 0 0 0 2.1-2.1 31 31 0 0 0 .5-4.8 31 31 0 0 0-.5-4.8z" /><path fill="#fff" d="m9.8 15.1 5.8-3.1-5.8-3.1z" /></svg>;
  if (platform === "kick")
    return <svg viewBox="0 0 24 24" className={cn("h-3.5 w-3.5", className)} aria-label="Kick"><rect width="24" height="24" rx="4" fill="#53fc18" /><path d="M6 5h4v4h2V7h2V5h4v5h-2v2h-2v0h2v2h2v5h-4v-2h-2v-2h-2v4H6z" fill="#0b0b10" /></svg>;
  return <svg viewBox="0 0 24 24" className={cn("h-3.5 w-3.5", className)} aria-label="Local file"><rect x="3" y="5" width="18" height="14" rx="2" fill="none" stroke="#9a9aae" strokeWidth="2" /><path d="m10 9 5 3-5 3z" fill="#9a9aae" /></svg>;
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="label">{label}</span>
      <div className="mt-1">{children}</div>
      {hint && <span className="mt-1 block text-[11px] text-ink-400">{hint}</span>}
    </label>
  );
}

export const inputCls = "focus-ring h-8 w-full border border-ink-600 bg-ink-900 px-2.5 text-sm text-ink-100 placeholder:text-ink-500 hover:border-ink-500";
