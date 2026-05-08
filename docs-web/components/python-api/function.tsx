"use client";

import { cn } from "@/lib/utils";
import { PythonSignature } from "./signature";

interface Parameter {
  name: string;
  annotation?: { raw?: string } | null;
  default?: string | null;
  description?: string | null;
  is_optional?: boolean;
}

interface Docstring {
  summary?: string | null;
  description?: string | null;
  parameters?: Parameter[];
  returns?: { annotation?: { raw?: string } | null; description?: string | null } | null;
  raises?: { type: string; description?: string | null }[];
  examples?: string[];
  notes?: string[];
  deprecated?: string | null;
  version_added?: string | null;
}

interface PythonFunctionProps {
  name: string;
  signature: {
    parameters?: Parameter[];
    returnAnnotation?: { raw?: string } | null;
    isAsync?: boolean;
    decorators?: string[];
  };
  docstring?: Docstring | null;
  description?: string | null;
  isMethod?: boolean;
  className?: string;
}

function ParamTable({ parameters }: { parameters: Parameter[] }) {
  if (!parameters.length) return null;

  return (
    <div className="mt-4">
      <h4 className="text-sm font-semibold mb-2">Parameters</h4>
      <div className="border rounded-lg overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-muted/50">
            <tr>
              <th className="text-left p-2 font-medium">Name</th>
              <th className="text-left p-2 font-medium">Type</th>
              <th className="text-left p-2 font-medium">Default</th>
              <th className="text-left p-2 font-medium">Description</th>
            </tr>
          </thead>
          <tbody>
            {parameters.map((param, i) => (
              <tr key={i} className="border-t">
                <td className="p-2 font-mono text-xs">
                  {param.name}
                  {param.is_optional && (
                    <span className="text-muted-foreground ml-1">(optional)</span>
                  )}
                </td>
                <td className="p-2 font-mono text-xs text-blue-600 dark:text-blue-400">
                  {param.annotation?.raw || "Any"}
                </td>
                <td className="p-2 font-mono text-xs text-muted-foreground">
                  {param.default || "-"}
                </td>
                <td className="p-2 text-xs">
                  {param.description || "-"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ReturnsSection({
  returns,
}: {
  returns: { annotation?: { raw?: string } | null; description?: string | null } | null;
}) {
  if (!returns) return null;

  return (
    <div className="mt-4">
      <h4 className="text-sm font-semibold mb-2">Returns</h4>
      <div className="text-sm">
        {returns.annotation?.raw && (
          <span className="font-mono text-xs text-blue-600 dark:text-blue-400">
            {returns.annotation.raw}
          </span>
        )}
        {returns.description && (
          <span className="ml-2 text-muted-foreground">{returns.description}</span>
        )}
      </div>
    </div>
  );
}

function RaisesSection({
  raises,
}: {
  raises: { type: string; description?: string | null }[];
}) {
  if (!raises.length) return null;

  return (
    <div className="mt-4">
      <h4 className="text-sm font-semibold mb-2">Raises</h4>
      <ul className="text-sm space-y-1">
        {raises.map((exc, i) => (
          <li key={i}>
            <span className="font-mono text-xs text-red-600 dark:text-red-400">
              {exc.type}
            </span>
            {exc.description && (
              <span className="ml-2 text-muted-foreground">{exc.description}</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function ExamplesSection({ examples }: { examples: string[] }) {
  if (!examples.length) return null;

  return (
    <div className="mt-4">
      <h4 className="text-sm font-semibold mb-2">Examples</h4>
      {examples.map((example, i) => (
        <pre
          key={i}
          className="bg-muted p-3 rounded-lg text-xs overflow-x-auto mt-2"
        >
          <code>{example}</code>
        </pre>
      ))}
    </div>
  );
}

export function PythonFunction({
  name,
  signature,
  docstring,
  description,
  isMethod = false,
  className,
}: PythonFunctionProps) {
  const summary = docstring?.summary || description;
  const params = docstring?.parameters || signature.parameters || [];

  return (
    <div
      className={cn(
        "border rounded-lg p-4 my-4",
        "bg-card",
        className
      )}
    >
      <div className="flex items-center gap-2 mb-2">
        <span className="text-xs font-medium px-2 py-0.5 rounded bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200">
          {isMethod ? "method" : "function"}
        </span>
        {signature.isAsync && (
          <span className="text-xs font-medium px-2 py-0.5 rounded bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200">
            async
          </span>
        )}
      </div>

      <PythonSignature
        name={name}
        parameters={signature.parameters}
        returnAnnotation={signature.returnAnnotation}
        isAsync={signature.isAsync}
        decorators={signature.decorators}
      />

      {summary && (
        <p className="mt-3 text-sm text-muted-foreground">{summary}</p>
      )}

      {docstring?.description && (
        <div className="mt-2 text-sm text-muted-foreground whitespace-pre-wrap">
          {docstring.description}
        </div>
      )}

      <ParamTable parameters={params} />
      <ReturnsSection returns={docstring?.returns || null} />
      <RaisesSection raises={docstring?.raises || []} />
      <ExamplesSection examples={docstring?.examples || []} />

      {docstring?.deprecated && (
        <div className="mt-3 p-2 bg-yellow-50 dark:bg-yellow-950 rounded text-xs text-yellow-800 dark:text-yellow-200">
          ⚠️ Deprecated: {docstring.deprecated}
        </div>
      )}
    </div>
  );
}
