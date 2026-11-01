"""Built-in tool: declare_output_path — declare an output directory.

Agent calls this tool to declare where it has placed output files.
The path is validated against sandbox config's outputs field, and
recorded in tool_called events for downstream steps to read.
"""


def register(registry):
    @registry.register(
        "declare_output_path",
        "Declare an output directory for downstream steps. "
        "Call after writing results to a shared workspace directory. "
        "The path must match a configured output in the sandbox config.",
        {"path": str},
        deps=["sandbox_config"],
    )
    def declare_output_path(path, sandbox_config=None):
        outputs = getattr(sandbox_config, "outputs", None) or {}
        if not outputs:
            raise PermissionError(
                "No outputs configured in sandbox config. "
                "Add outputs to sandbox.yaml."
            )

        # Find a matching output entry (prefix match)
        for name, output_path in outputs.items():
            if path.startswith(output_path) or path == output_path:
                return f"Output declared: {path}"

        raise PermissionError(
            f"Path '{path}' is not a valid output directory. "
            f"Configured outputs: {list(outputs.keys())}"
        )
