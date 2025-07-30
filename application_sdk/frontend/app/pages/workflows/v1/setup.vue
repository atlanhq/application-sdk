<script setup lang="ts">
    // libraries
    import { Heading, Link, Stepper, Button, toast } from '@atlanhq/atlantis'

    const { data: fetchedWorkflowTemplate } = await useAsyncData(
        'workflow',
        () => $fetch('/workflows/v1/configmap/workflow')
    )

    const workflowTemplate = computed(() => fetchedWorkflowTemplate.value.data)

    const currentStep = ref(1)

    const steps = computed(() => workflowTemplate.value.config.steps || [])

    const currentStepConfig = computed(() => {
        if (steps.value) {
            return steps.value[currentStep.value - 1]
        }

        return {}
    })

    const formattedSteps = computed(() =>
        steps.value.map((step) => ({
            ...step,
            value: step.id,
        }))
    )

    const isValidating = ref(false)
    const formState = ref({})
    const workflowSubmissionError = ref('')
    const workflowSubmissionSuccess = ref(false)

    const formRefEl = useTemplateRef('formRef')

    async function handleNext() {
        isValidating.value = true
        workflowSubmissionError.value = ''

        try {
            const formValues = await formRefEl.value.validate()

            formState.value = {
                ...formState.value,
                [currentStepConfig.value.id]: formValues,
            }

            if (currentStep.value === steps.value.length) {
                // Submit workflow with collected form data
                try {
                    await $fetch('/workflows/v1/start', {
                        method: 'POST',
                        body: {
                            ...formState.value,
                            connection: {
                                connection_name: 'test-connection',
                                connection_qualified_name: `default/${workflowTemplate.value.id}/${Math.round(new Date().getTime() / 1000)}`,
                            },
                            credential: formValues.credential_guid_body,
                        },
                    })

                    toast.success('Workflow started successfully!', {
                        placement: 'top',
                    })

                    currentStep.value = 1
                } catch (apiError) {
                    toast.failed(
                        'Failed to start workflow: ' + apiError.message,
                        {
                            placement: 'top',
                        }
                    )
                }

                return
            }

            currentStep.value = currentStep.value + 1
        } catch (error) {
            toast.failed('Failed to validate form: ' + error.message, {
                placement: 'top',
            })
        } finally {
            isValidating.value = false
        }
    }
</script>

<template>
    <div class="min-h-screen bg-gray-50 py-8 flex flex-col">
        <div class="w-[700px] mx-auto px-4 flex flex-col flex-1">
            <!-- Header -->
            <div class="flex items-center justify-between mb-8">
                <div class="flex items-center gap-4">
                    <img
                        :src="workflowTemplate.logo"
                        alt="PostgreSQL Logo"
                        class="w-16 h-16"
                    />
                    <Heading as="h1" size="xl">
                        {{ workflowTemplate.name }} App
                    </Heading>
                </div>
                <Link
                    type="default"
                    size="default"
                    label="Documentation"
                    url="https://ask.atlan.com/hc/en-us/articles/6329557275793-How-to-crawl-PostgreSQL"
                    :isOpenInNewTab="true"
                    class="flex items-center gap-2 px-4 py-2 border border-gray-300 rounded-lg hover:bg-gray-50"
                >
                    <template #prefixIcon>
                        <svg
                            xmlns="http://www.w3.org/2000/svg"
                            fill="none"
                            viewBox="0 0 24 24"
                            stroke-width="1.5"
                            stroke="currentColor"
                            class="w-5 h-5"
                        >
                            <path
                                stroke-linecap="round"
                                stroke-linejoin="round"
                                d="M12 6.042A8.967 8.967 0 0 0 6 3.75c-1.052 0-2.062.18-3 .512v14.25A8.987 8.987 0 0 1 6 18c2.305 0 4.408.867 6 2.292m0-14.25a8.966 8.966 0 0 1 6-2.292c1.052 0 2.062.18 3 .512v14.25A8.987 8.987 0 0 0 18 18a8.967 8.967 0 0 0-6 2.292m0-14.25v14.25"
                            />
                        </svg>
                    </template>
                </Link>
            </div>

            <div class="mb-8">
                <Stepper
                    :items="formattedSteps"
                    :modelValue="currentStep"
                    orientation="horizontal"
                    type="default"
                    @stepChange="(step) => (currentStep = step)"
                />
            </div>

            <div class="bg-white rounded-lg shadow-sm p-8 flex gap-8 grow">
                <div class="flex-1 w-full">
                    <DynamicFormWrapper
                        ref="formRef"
                        :currentStep="currentStepConfig"
                        :configMap="workflowTemplate.config"
                    />
                </div>
            </div>

            <!-- Success Message -->
            <div
                v-if="workflowSubmissionSuccess"
                class="bg-green-50 border border-green-200 rounded-lg p-4 mb-4"
            >
                <div class="flex items-center">
                    <svg
                        class="w-5 h-5 text-green-600 mr-3"
                        fill="currentColor"
                        viewBox="0 0 20 20"
                    >
                        <path
                            fill-rule="evenodd"
                            d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z"
                            clip-rule="evenodd"
                        />
                    </svg>
                    <p class="text-green-800 font-medium">
                        Workflow started successfully! Your PokéShift adventure
                        has begun.
                    </p>
                </div>
            </div>

            <!-- Error Message -->
            <div
                v-if="workflowSubmissionError"
                class="bg-red-50 border border-red-200 rounded-lg p-4 mb-4"
            >
                <div class="flex items-center">
                    <svg
                        class="w-5 h-5 text-red-600 mr-3"
                        fill="currentColor"
                        viewBox="0 0 20 20"
                    >
                        <path
                            fill-rule="evenodd"
                            d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z"
                            clip-rule="evenodd"
                        />
                    </svg>
                    <p class="text-red-800 font-medium">
                        {{ workflowSubmissionError }}
                    </p>
                </div>
            </div>

            <div class="flex justify-between py-3 border-t">
                <Button
                    v-if="currentStep > 0"
                    type="subtle"
                    label="Back"
                    @click="
                        () => {
                            currentStep = currentStep - 1
                        }
                    "
                />

                <Button
                    v-if="currentStep < steps.length"
                    label="Next"
                    class="ml-auto"
                    :isLoading="isValidating"
                    @click="handleNext"
                />

                <div
                    v-else-if="
                        currentStep === steps.length &&
                        !workflowSubmissionSuccess
                    "
                    class="flex ml-auto gap-x-2"
                >
                    <Button
                        v-if="!workflowSubmissionError"
                        label="Start Workflow"
                        :isLoading="isValidating"
                        @click="handleNext"
                    />
                    <Button
                        v-else
                        label="Retry"
                        type="subtle"
                        :isLoading="isValidating"
                        @click="handleNext"
                    />
                </div>

                <div
                    v-else-if="workflowSubmissionSuccess"
                    class="flex ml-auto gap-x-2"
                >
                    <Button
                        label="Workflow Started!"
                        type="success"
                        :disabled="true"
                    />
                </div>
            </div>
        </div>
    </div>
</template>
