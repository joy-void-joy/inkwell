// Controls rendered from an entry point's descriptor, fetched from
// GET /api/entry-points. Nothing here names a parameter: a control is chosen by
// the widget the declaration asked for, so a scalar or boolean parameter added
// to a declaration appears on the form with no edit to this file. The bespoke
// controls a descriptor cannot describe — the source picker, the format
// description, the per-stage model grid — stay hand-written on their page and
// are marked `rendered: false` so they are skipped here.
import { useEffect, useState } from "react";
import { fetchOptions } from "../api/client";
import type {
  ParameterDescriptor,
  SuppliedValue,
  SuppliedValues,
} from "../types";

function asText(value: SuppliedValue): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.join("\n");
  return "";
}

function OptionSelect({
  parameter,
  value,
  onChange,
}: {
  parameter: ParameterDescriptor;
  value: SuppliedValue;
  onChange: (next: SuppliedValue) => void;
}) {
  const [options, setOptions] = useState<string[]>([]);

  useEffect(() => {
    if (!parameter.options_endpoint) return;
    fetchOptions(parameter.options_endpoint).then(setOptions).catch(() => {});
  }, [parameter.options_endpoint]);

  return (
    <select
      id={`declared-${parameter.name}`}
      value={asText(value)}
      onChange={(e) => onChange(e.target.value || null)}
    >
      {!parameter.required && <option value="">Not set</option>}
      {options.map((option) => (
        <option key={option} value={option}>
          {option}
        </option>
      ))}
    </select>
  );
}

function DeclaredControl({
  parameter,
  value,
  onChange,
}: {
  parameter: ParameterDescriptor;
  value: SuppliedValue;
  onChange: (next: SuppliedValue) => void;
}) {
  switch (parameter.widget) {
    case "select":
      return (
        <OptionSelect parameter={parameter} value={value} onChange={onChange} />
      );
    case "textarea":
      return (
        <textarea
          id={`declared-${parameter.name}`}
          value={asText(value)}
          onChange={(e) => onChange(e.target.value)}
          rows={4}
          required={parameter.required}
        />
      );
    case "lines":
      return (
        <textarea
          id={`declared-${parameter.name}`}
          value={asText(value)}
          onChange={(e) =>
            onChange(
              e.target.value
                .split("\n")
                .map((line) => line.trim())
                .filter(Boolean),
            )
          }
          rows={3}
          required={parameter.required}
        />
      );
    default:
      return (
        <input
          id={`declared-${parameter.name}`}
          type="text"
          value={asText(value)}
          onChange={(e) => onChange(e.target.value || null)}
          required={parameter.required}
        />
      );
  }
}

function DeclaredField({
  parameter,
  value,
  onChange,
}: {
  parameter: ParameterDescriptor;
  value: SuppliedValue;
  onChange: (next: SuppliedValue) => void;
}) {
  if (parameter.widget === "flag") {
    return (
      <div className="form-group form-group-flag">
        <label htmlFor={`declared-${parameter.name}`}>
          <input
            id={`declared-${parameter.name}`}
            type="checkbox"
            checked={value === true}
            onChange={(e) => onChange(e.target.checked)}
          />{" "}
          {parameter.label}
        </label>
        {parameter.help && (
          <p className="form-note" style={{ opacity: 0.7 }}>
            {parameter.help}
          </p>
        )}
      </div>
    );
  }

  return (
    <div className="form-group">
      <label htmlFor={`declared-${parameter.name}`}>
        {parameter.label}
        {parameter.required ? "" : " (optional)"}
      </label>
      <DeclaredControl
        parameter={parameter}
        value={value}
        onChange={onChange}
      />
      {parameter.help && (
        <p className="form-note" style={{ opacity: 0.7 }}>
          {parameter.help}
        </p>
      )}
    </div>
  );
}

export function DeclaredFields({
  parameters,
  values,
  onChange,
  only,
}: {
  parameters: ParameterDescriptor[];
  values: SuppliedValues;
  onChange: (name: string, next: SuppliedValue) => void;
  // Render just these parameters, in this order, when a page places some of
  // them itself; every generic one, in declared order, when absent.
  only?: string[];
}) {
  const generic = parameters.filter((p) => p.rendered);
  const shown = only
    ? only.flatMap((name) => generic.filter((p) => p.name === name))
    : generic;

  return (
    <>
      {shown.map((parameter) => (
        <DeclaredField
          key={parameter.name}
          parameter={parameter}
          value={values[parameter.name] ?? parameter.default}
          onChange={(next) => onChange(parameter.name, next)}
        />
      ))}
    </>
  );
}
