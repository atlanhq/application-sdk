<script setup lang="ts">
    // libraries
    import { Heading, Stepper, Button, toast } from '@atlanhq/atlantis'

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
