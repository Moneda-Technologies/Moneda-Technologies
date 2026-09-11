/**
 * Monotonic identity for customer-context transitions.
 *
 * Pages may finish loading after the user has selected another customer. A
 * completed request is only allowed to update the UI when its transition
 * identity is still current.
 */
let contextRevision = 0;
let transitionController: AbortController | null = null;

export function beginCustomerContextChange(): number {
  transitionController?.abort();
  transitionController = new AbortController();
  contextRevision += 1;
  return contextRevision;
}

export function customerContextSignal(): AbortSignal {
  if (!transitionController) transitionController = new AbortController();
  return transitionController.signal;
}

export function currentCustomerContextRevision(): number {
  return contextRevision;
}

export function isCurrentCustomerContextRevision(revision: number): boolean {
  return revision === contextRevision;
}
