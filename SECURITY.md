# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |
| < 0.1   | :x:                |

## Reporting a Vulnerability

If you discover a security vulnerability in Bedrock, please report it responsibly.

### How to Report

**Do NOT open a public GitHub issue for security vulnerabilities.**

Instead, please send an email to the project maintainers with:

1. **Description**: A clear description of the vulnerability
2. **Impact**: Potential impact and attack scenarios
3. **Reproduction**: Steps to reproduce the vulnerability
4. **Affected Versions**: Which versions are affected
5. **Suggested Fix**: If you have a fix in mind (optional)

### What to Expect

- **Acknowledgment**: We will acknowledge receipt within 48 hours
- **Assessment**: We will assess the vulnerability and determine severity
- **Fix Timeline**: We aim to release fixes within 7 days for critical vulnerabilities
- **Disclosure**: We will coordinate disclosure with you before making it public

### Safe Harbor

We support responsible disclosure and will not take legal action against researchers who:

- Make a good faith effort to avoid privacy violations
- Only interact with accounts you own or with explicit permission
- Do not exploit a vulnerability beyond what is necessary to confirm its existence
- Provide us with reasonable time to resolve the issue before public disclosure

## Security Best Practices for Users

When using Bedrock in production:

1. **Keep Updated**: Always use the latest stable version
2. **Pin Dependencies**: Use exact versions in production
3. **Review Dependencies**: Regularly audit your dependency tree
4. **Use Trusted Sources**: Only install from PyPI or trusted repositories
5. **Environment Variables**: Never commit secrets or credentials

## Security-Related Configuration

Bedrock includes security-conscious defaults:

- **Database**: Uses parameterized queries via SQLAlchemy
- **Settings**: Supports environment variable overrides for secrets
- **Validation**: Pydantic models validate all inputs

## Contact

For security concerns, contact the project maintainers directly via email.

For non-security issues, please use [GitHub Issues](https://github.com/maacck/bedrock/issues).
