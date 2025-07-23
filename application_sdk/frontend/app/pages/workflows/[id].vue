<script setup lang="ts">
    // libraries
    import { Heading, Link, Stepper, Button } from '@atlanhq/atlantis'

    // components
    import { FormStateInjectionKey } from '~/constants/workflows'

    const route = useRoute()

    const { data: fetchedWorkflowTemplate } = await useAsyncData(() =>
        queryCollection('workflows')
            .where('stem', 'LIKE', `%${route.params.id as string}`)
            .first()
    )

    const workflowTemplate = computed(() => fetchedWorkflowTemplate.value.body)

    const currentStep = ref(0)
    const modelValue = ref({})

    provide(FormStateInjectionKey, modelValue)

    const steps = computed(() => workflowTemplate.value.config.steps || [])

    const currentStepConfig = computed(() => {
        if (steps.value) {
            return steps.value[currentStep.value]
        }

        return {}
    })

    const formattedSteps = computed(() =>
        steps.value.map((step) => ({
            ...step,
            value: step.id,
        }))
    )

    const handleSubmit = async () => {
        //
    }
</script>

<template>
    <div class="min-h-screen bg-gray-50 py-8">
        <div class="w-[700px] mx-auto px-4">
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

            <div class="bg-white rounded-lg shadow-sm p-8 flex gap-8">
                <div class="flex-1 w-full">
                    <DynamicForm
                        :modelValue="modelValue[currentStepConfig.id]"
                        :currentStep="currentStepConfig"
                        :configMap="workflowTemplate.config"
                        @update:modelValue="
                            (value) => {
                                modelValue = {
                                    ...modelValue,
                                    [currentStepConfig.id]: value,
                                }
                            }
                        "
                    />
                </div>
            </div>

            <div class="flex justify-between py-3 border-t">
                <Button
                    v-if="currentStep > 1"
                    type="subtle"
                    label="Back"
                    @click="
                        () => {
                            currentStep = currentStep - 1
                        }
                    "
                />

                <Button
                    v-if="currentStep < steps.length - 1"
                    label="Next"
                    class="ml-auto"
                    @click="
                        () => {
                            currentStep = currentStep + 1
                        }
                    "
                />

                <div
                    v-else-if="currentStep === steps.length - 1"
                    class="flex ml-auto gap-x-2"
                >
                    <Button label="Done" @click="handleSubmit" />
                </div>
            </div>
        </div>
    </div>
</template>
