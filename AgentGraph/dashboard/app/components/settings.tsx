"use client";

import type { ReactNode } from "react";

/*
 * Settings layout: sections listed on the left, the open section on the right.
 * Each section is a title, one line that says what it is for, then groups of
 * rows. A row reads like a sentence on the left (what it is, what it does) and
 * holds its control on the right. Grouped lists, not cards (DESIGN.md).
 */

export type SettingsNavItem = { id: string; label: string; count?: number; alert?: number };
export type SettingsNavGroup = { label: string; items: SettingsNavItem[] };

export function SettingsFrame({
  groups,
  active,
  onSelect,
  title,
  description,
  navLabel,
  children,
}: {
  groups: SettingsNavGroup[];
  active: string;
  onSelect: (id: string) => void;
  title: string;
  description?: string;
  navLabel: string;
  children: ReactNode;
}) {
  return (
    <div className="settings">
      <nav className="settings-nav" aria-label={navLabel}>
        {groups.map((group) => (
          <div key={group.label} className="settings-nav-group">
            <div className="settings-nav-label">{group.label}</div>
            {group.items.map((item) => (
              <button
                key={item.id}
                type="button"
                className={`settings-nav-item${active === item.id ? " active" : ""}`}
                aria-current={active === item.id ? "page" : undefined}
                onClick={() => onSelect(item.id)}
              >
                <span>{item.label}</span>
                {typeof item.count === "number" && item.count > 0 && (
                  <span className={`settings-count${item.alert ? " alert" : ""}`}>{item.count}</span>
                )}
              </button>
            ))}
          </div>
        ))}
      </nav>
      <div className="settings-body">
        <header className="settings-head">
          <h1>{title}</h1>
          {description && <p>{description}</p>}
        </header>
        {children}
      </div>
    </div>
  );
}

export function SettingsGroup({
  title,
  description,
  action,
  children,
  id,
}: {
  title?: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  id?: string;
}) {
  return (
    <section className="settings-group" id={id}>
      {(title || action) && (
        <header className="settings-group-head">
          <div>
            {title && <h2>{title}</h2>}
            {description && <p>{description}</p>}
          </div>
          {action && <div className="settings-group-action">{action}</div>}
        </header>
      )}
      <div className="settings-rows">{children}</div>
    </section>
  );
}

export function SettingRow({
  label,
  description,
  children,
  htmlFor,
  lamp,
  stack = false,
  tone,
}: {
  label: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  htmlFor?: string;
  /** A state lamp before the label: green, amber, red, cyan, or "" for off. */
  lamp?: string;
  /** Put the control under the text (long values, code, lists). */
  stack?: boolean;
  tone?: "need";
}) {
  const Label = htmlFor ? "label" : "span";
  return (
    <div className={`setting-row${stack ? " stack" : ""}${tone === "need" ? " need" : ""}`}>
      <div className="setting-text">
        <Label className="setting-label" {...(htmlFor ? { htmlFor } : {})}>
          {lamp !== undefined && <i className={`lamp ${lamp}`} />}
          {label}
        </Label>
        {description && <div className="setting-desc">{description}</div>}
      </div>
      {children !== undefined && children !== null && children !== false && (
        <div className="setting-control">{children}</div>
      )}
    </div>
  );
}

export function SettingsFooter({ children }: { children: ReactNode }) {
  return <div className="settings-footer">{children}</div>;
}

export function Switch({
  checked,
  onChange,
  label,
  disabled = false,
  id,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
  id?: string;
}) {
  return (
    <button
      id={id}
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      className={`switch${checked ? " on" : ""}`}
      onClick={() => onChange(!checked)}
    >
      <span className="switch-thumb" />
    </button>
  );
}
