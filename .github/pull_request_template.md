## Pull Request Template

### JIRA Ticket
<!-- MANDATORY: Link to associated JIRA ticket -->
- **Ticket Number**: [PROJ-XXX]
- [ ] Ticket is linked and referenced in the PR title
- [ ] PR description references the full ticket URL

### Description
<!-- Provide a clear and concise description of the changes in this PR -->
- What is the purpose of these changes?
- What problem does this PR solve?
- Are there any breaking changes?
- Link to JIRA ticket details: [Full JIRA Ticket URL]
- Mermaid diagram if its a complex PR

### Type of Change
<!-- Mark [x] the appropriate option -->
- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to change)
- [ ] Documentation update
- [ ] Performance improvement
- [ ] Refactoring

### How Has This Been Tested?
<!-- Describe the tests you've added/modified to verify your changes -->
- Added/modified unit tests: [ ] Yes / [ ] No
- Added/modified integration tests: [ ] Yes / [ ] No
- Test coverage percentage: 
- Tested scenarios:
  1. 
  2. 
  3. 

### Checklist
<!-- Confirm that your code meets the following requirements -->
- [ ] I have linked the relevant JIRA ticket
- [ ] I have performed a self-review of my own code
- [ ] I have commented my code, particularly in hard-to-understand areas
- [ ] I have added tests that prove my fix is effective or that my feature works
- [ ] My changes generate no new warnings
- [ ] I have updated the documentation accordingly
- [ ] My code follows the project's style guidelines
- [ ] I have added type hints to function signatures
- [ ] I have run `mypy` and `pylint` with no errors

### SDK Specific Considerations
<!-- SDK-specific checks and documentation -->
- [ ] Updated `requirements.txt` or `setup.py` if new dependencies were added
- [ ] Updated API documentation
- [ ] Ensured backward compatibility
- [ ] Verified error handling and logging
- [ ] Checked performance implications

### Additional Context
<!-- Add any other context about the PR here -->
- Related JIRA Ticket: [PROJ-XXX]
- Related issues: 
- Screenshots (if applicable):
- Performance impact:
- Security considerations:

### Reviewer Checklist
<!-- For reviewers to check -->
- [ ] JIRA ticket is properly linked and referenced
- [ ] Code quality and readability
- [ ] Comprehensive test coverage
- [ ] Proper error handling
- [ ] Performance considerations
- [ ] Documentation completeness

### PR Naming Convention
<!-- Recommended PR title format -->
**Recommended Title Format**: 
`[PROJ-XXX] Brief description of changes`

**Example**: 
`[PROJ-1234] Add authentication middleware to SDK`
