"use client";

import { cn } from "@/lib/utils";
import { PythonFunction } from "./function";
import { PythonSignature } from "./signature";

interface Parameter {
  name: string;
  annotation?: { raw?: string } | null;
  default?: string | null;
  description?: string | null;
  is_optional?: boolean;
}

interface Member {
  name: string;
  kind: string;
  signature?: {
    parameters?: Parameter[];
    returnAnnotation?: { raw?: string } | null;
    isAsync?: boolean;
    decorators?: string[];
  } | null;
  docstring?: {
    summary?: string | null;
    description?: string | null;
    parameters?: Parameter[];
    returns?: { annotation?: { raw?: string } | null; description?: string | null } | null;
    raises?: { type: string; description?: string | null }[];
    examples?: string[];
  } | null;
  description?: string | null;
  base_classes?: string[];
  members?: Member[];
}

interface Docstring {
  summary?: string | null;
  description?: string | null;
  parameters?: Parameter[];
  examples?: string[];
  notes?: string[];
}

interface PythonClassProps {
  name: string;
  members?: Member[];
  baseClasses?: string[];
  docstring?: Docstring | null;
  description?: string | null;
  isAbstract?: boolean;
  className?: string;
}

function BaseClasses({ classes }: { classes: string[] }) {
  if (!classes.length) return null;

  return (
    <div className="text-sm text-muted-foreground">
      <span className="font-medium">Inherits from: </span>
      {classes.map((cls, i) => (
        <span key={i}>
          <span className="font-mono text-xs text-blue-600 dark:text-blue-400">
            {cls}
          </span>
          {i < classes.length - 1 && ", "}
        </span>
      ))}
    </div>
  );
}

function MemberList({ members }: { members: Member[] }) {
  const methods = members.filter((m) => m.kind === "method");
  const properties = members.filter((m) => m.kind === "property");
  const classmethods = members.filter((m) =>
    m.signature?.decorators?.includes("classmethod")
  );
  const staticmethods = members.filter((m) =>
    m.signature?.decorators?.includes("staticmethod")
  );

  return (
    <div className="mt-6 space-y-6">
      {classmethods.length > 0 && (
        <div>
          <h3 className="text-lg font-semibold mb-3">Class Methods</h3>
          {classmethods.map((member) => (
            <PythonFunction
              key={member.name}
              name={member.name}
              signature={member.signature || { parameters: [] }}
              docstring={member.docstring}
              description={member.description}
              isMethod
            />
          ))}
        </div>
      )}

      {staticmethods.length > 0 && (
        <div>
          <h3 className="text-lg font-semibold mb-3">Static Methods</h3>
          {staticmethods.map((member) => (
            <PythonFunction
              key={member.name}
              name={member.name}
              signature={member.signature || { parameters: [] }}
              docstring={member.docstring}
              description={member.description}
              isMethod
            />
          ))}
        </div>
      )}

      {properties.length > 0 && (
        <div>
          <h3 className="text-lg font-semibold mb-3">Properties</h3>
          {properties.map((member) => (
            <div
              key={member.name}
              className="border rounded-lg p-4 my-2 bg-card"
            >
              <div className="flex items-center gap-2 mb-2">
                <span className="text-xs font-medium px-2 py-0.5 rounded bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200">
                  property
                </span>
              </div>
              <div className="font-mono text-sm">
                <span className="text-amber-600 dark:text-amber-400 font-bold">
                  {member.name}
                </span>
              </div>
              {(member.docstring?.summary || member.description) && (
                <p className="mt-2 text-sm text-muted-foreground">
                  {member.docstring?.summary || member.description}
                </p>
              )}
            </div>
          ))}
        </div>
      )}

      {methods.length > 0 && (
        <div>
          <h3 className="text-lg font-semibold mb-3">Methods</h3>
          {methods.map((member) => (
            <PythonFunction
              key={member.name}
              name={member.name}
              signature={member.signature || { parameters: [] }}
              docstring={member.docstring}
              description={member.description}
              isMethod
            />
          ))}
        </div>
      )}
    </div>
  );
}

export function PythonClass({
  name,
  members = [],
  baseClasses = [],
  docstring,
  description,
  isAbstract = false,
  className,
}: PythonClassProps) {
  const summary = docstring?.summary || description;

  return (
    <div
      className={cn(
        "border rounded-lg p-6 my-6",
        "bg-card",
        className
      )}
    >
      <div className="flex items-center gap-2 mb-3">
        <span className="text-xs font-medium px-2 py-0.5 rounded bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-200">
          class
        </span>
        {isAbstract && (
          <span className="text-xs font-medium px-2 py-0.5 rounded bg-gray-100 text-gray-800 dark:bg-gray-900 dark:text-gray-200">
            abstract
          </span>
        )}
      </div>

      <h2 className="text-2xl font-bold mb-2">
        <span className="font-mono">{name}</span>
      </h2>

      <BaseClasses classes={baseClasses} />

      {summary && (
        <p className="mt-3 text-muted-foreground">{summary}</p>
      )}

      {docstring?.description && (
        <div className="mt-2 text-sm text-muted-foreground whitespace-pre-wrap">
          {docstring.description}
        </div>
      )}

      {docstring?.notes && docstring.notes.length > 0 && (
        <div className="mt-4 p-3 bg-blue-50 dark:bg-blue-950 rounded-lg">
          <h4 className="text-sm font-semibold mb-1">Notes</h4>
          {docstring.notes.map((note, i) => (
            <p key={i} className="text-sm text-blue-800 dark:text-blue-200">
              {note}
            </p>
          ))}
        </div>
      )}

      <MemberList members={members} />
    </div>
  );
}
