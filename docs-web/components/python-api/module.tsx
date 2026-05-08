"use client";

import { cn } from "@/lib/utils";
import { PythonClass } from "./class";
import { PythonFunction } from "./function";

interface Member {
  name: string;
  kind: string;
  signature?: {
    parameters?: { name: string; annotation?: { raw?: string } | null; default?: string | null }[];
    returnAnnotation?: { raw?: string } | null;
    isAsync?: boolean;
    decorators?: string[];
  } | null;
  docstring?: {
    summary?: string | null;
    description?: string | null;
    parameters?: { name: string; annotation?: { raw?: string } | null; default?: string | null; description?: string | null }[];
    returns?: { annotation?: { raw?: string } | null; description?: string | null } | null;
    raises?: { type: string; description?: string | null }[];
    examples?: string[];
  } | null;
  description?: string | null;
  base_classes?: string[];
  members?: Member[];
}

interface Submodule {
  name: string;
  qualified_name: string;
  description?: string | null;
  members?: Member[];
}

interface PythonModuleProps {
  name: string;
  qualifiedName: string;
  description?: string | null;
  docstring?: { summary?: string | null; description?: string | null } | null;
  members?: Member[];
  submodules?: Submodule[];
  className?: string;
}

function ModuleMembers({ members }: { members: Member[] }) {
  const classes = members.filter((m) => m.kind === "class" || m.kind === "exception");
  const functions = members.filter((m) => m.kind === "function");
  const constants = members.filter((m) => m.kind === "constant" || m.kind === "attribute");

  return (
    <div className="space-y-8">
      {constants.length > 0 && (
        <section>
          <h3 className="text-xl font-semibold mb-4 border-b pb-2">
            Constants & Attributes
          </h3>
          <div className="grid gap-2">
            {constants.map((member) => (
              <div
                key={member.name}
                className="flex items-center gap-4 p-3 border rounded-lg bg-card"
              >
                <code className="font-mono text-sm font-semibold text-amber-600 dark:text-amber-400">
                  {member.name}
                </code>
                {member.description && (
                  <span className="text-sm text-muted-foreground truncate">
                    {member.description}
                  </span>
                )}
              </div>
            ))}
          </div>
        </section>
      )}

      {classes.length > 0 && (
        <section>
          <h3 className="text-xl font-semibold mb-4 border-b pb-2">
            Classes
          </h3>
          {classes.map((member) => (
            <PythonClass
              key={member.name}
              name={member.name}
              members={member.members}
              baseClasses={member.base_classes}
              docstring={member.docstring}
              description={member.description}
            />
          ))}
        </section>
      )}

      {functions.length > 0 && (
        <section>
          <h3 className="text-xl font-semibold mb-4 border-b pb-2">
            Functions
          </h3>
          {functions.map((member) => (
            <PythonFunction
              key={member.name}
              name={member.name}
              signature={member.signature || { parameters: [] }}
              docstring={member.docstring}
              description={member.description}
            />
          ))}
        </section>
      )}
    </div>
  );
}

function SubmoduleList({ submodules }: { submodules: Submodule[] }) {
  if (!submodules.length) return null;

  return (
    <section className="mt-8">
      <h3 className="text-xl font-semibold mb-4 border-b pb-2">
        Submodules
      </h3>
      <div className="grid gap-3">
        {submodules.map((submod) => (
          <a
            key={submod.name}
            href={`#${submod.name}`}
            className="block p-4 border rounded-lg hover:bg-accent transition-colors"
          >
            <div className="font-mono text-sm font-semibold text-blue-600 dark:text-blue-400">
              {submod.qualified_name}
            </div>
            {submod.description && (
              <p className="mt-1 text-sm text-muted-foreground truncate">
                {submod.description}
              </p>
            )}
          </a>
        ))}
      </div>
    </section>
  );
}

export function PythonModule({
  name,
  qualifiedName,
  description,
  docstring,
  members = [],
  submodules = [],
  className,
}: PythonModuleProps) {
  const summary = docstring?.summary || description;

  return (
    <div className={cn("max-w-4xl", className)}>
      <div className="mb-8">
        <div className="flex items-center gap-2 mb-2">
          <span className="text-xs font-medium px-2 py-0.5 rounded bg-gray-100 text-gray-800 dark:bg-gray-900 dark:text-gray-200">
            module
          </span>
        </div>

        <h1 className="text-3xl font-bold mb-2">
          <code>{qualifiedName}</code>
        </h1>

        {summary && (
          <p className="text-lg text-muted-foreground">{summary}</p>
        )}

        {docstring?.description && (
          <div className="mt-4 text-sm text-muted-foreground whitespace-pre-wrap">
            {docstring.description}
          </div>
        )}
      </div>

      <SubmoduleList submodules={submodules} />
      <ModuleMembers members={members} />
    </div>
  );
}
