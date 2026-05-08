"use client";

import { cn } from "@/lib/utils";

interface Parameter {
  name: string;
  annotation?: { raw?: string } | null;
  default?: string | null;
  is_optional?: boolean;
}

interface SignatureProps {
  name: string;
  parameters?: Parameter[];
  returnAnnotation?: { raw?: string } | null;
  isAsync?: boolean;
  decorators?: string[];
  className?: string;
}

function formatParam(param: Parameter): string {
  let result = param.name;
  if (param.annotation?.raw) {
    result += `: ${param.annotation.raw}`;
  }
  if (param.default) {
    result += ` = ${param.default}`;
  }
  return result;
}

export function PythonSignature({
  name,
  parameters = [],
  returnAnnotation,
  isAsync = false,
  decorators = [],
  className,
}: SignatureProps) {
  const formattedParams = parameters.map(formatParam);
  const paramStr = formattedParams.join(", ");
  const returnStr = returnAnnotation?.raw ? ` -> ${returnAnnotation.raw}` : "";

  return (
    <div className={cn("font-mono text-sm", className)}>
      {decorators.map((dec, i) => (
        <div key={i} className="text-muted-foreground">
          @{dec}
        </div>
      ))}
      <div>
        {isAsync && (
          <span className="text-purple-500 font-semibold">async </span>
        )}
        <span className="text-blue-600 dark:text-blue-400 font-semibold">
          def{" "}
        </span>
        <span className="text-amber-600 dark:text-amber-400 font-bold">
          {name}
        </span>
        <span className="text-muted-foreground">({paramStr})</span>
        {returnStr && (
          <span className="text-emerald-600 dark:text-emerald-400">
            {returnStr}
          </span>
        )}
        :
      </div>
    </div>
  );
}
